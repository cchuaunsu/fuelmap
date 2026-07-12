"""Refresh Orchestrator — one manual Refresh = one complete investigation.

    Discovery -> Providers -> Normalizer -> Station Matching ->
    Verification -> Confidence -> Conflict Resolution -> Verified Price Store

The orchestrator owns sequencing and the per-refresh context only; every
stage lives in its own engine and is independently replaceable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fie.collection.collector import EvidenceCollector
from fie.config import Settings
from fie.confidence.engine import ConfidenceEngine
from fie.context import RefreshContext
from fie.derivation.engine import DerivationEngine
from fie.discovery.engine import DiscoveryEngine
from fie.providers.adjustments import BaseAdjustmentProvider
from fie.models.enums import FuelType, ResolutionStatus
from fie.models.evidence import EvidenceRejection, NormalizedEvidence
from fie.models.verification import ResolvedPrice
from fie.matching.engine import StationMatchingEngine
from fie.normalization.engine import NormalizationEngine
from fie.observability import get_logger
from fie.record import LedgerView
from fie.resolution.engine import ConflictResolutionEngine
from fie.stationdb.repository import StationRepository
from fie.store.price_store import VerifiedPriceStore
from fie.verification.engine import VerificationEngine

log = get_logger("pipeline")


@dataclass
class RefreshReport:
    run_id: str
    started_at: str
    finished_at: str
    stations_processed: int
    results: list[ResolvedPrice] = field(default_factory=list)
    store_actions: dict[str, str] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    provider_errors: list[dict[str, str]] = field(default_factory=list)
    trace: dict[str, Any] | None = None


class RefreshOrchestrator:
    def __init__(
        self,
        settings: Settings,
        stations: StationRepository,
        discovery: DiscoveryEngine,
        collector: EvidenceCollector,
        normalizer: NormalizationEngine,
        verifier: VerificationEngine,
        confidence: ConfidenceEngine,
        resolver: ConflictResolutionEngine,
        store: VerifiedPriceStore,
        derivation: DerivationEngine | None = None,
        adjustment_providers: list[BaseAdjustmentProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._stations = stations
        self._discovery = discovery
        self._collector = collector
        self._normalizer = normalizer
        self._verifier = verifier
        self._confidence = confidence
        self._resolver = resolver
        self._store = store
        self._derivation = derivation
        self._adjustment_providers = adjustment_providers or []

    async def refresh(
        self,
        station_ids: list[str] | None = None,
        developer_mode: bool = False,
    ) -> RefreshReport:
        stations = self._stations.get_all()
        if station_ids:
            wanted = set(station_ids)
            stations = [s for s in stations if s.station_id in wanted]

        # Store calls are synchronous; against a remote Postgres they take
        # real wall-clock time, so every one of them runs in a worker
        # thread to keep the event loop (and /health) responsive.
        ctx = RefreshContext(
            settings=self._settings,
            developer_mode=developer_mode and self._settings.developer_mode_enabled,
            provider_reliability=await asyncio.to_thread(
                self._store.get_provider_reliability
            ),
        )
        log.info(
            "Refresh %s started for %d stations", ctx.run_id, len(stations)
        )
        try:
            return await self._run(stations, ctx)
        finally:
            # End of investigation: the per-refresh cache and connection
            # pool are destroyed; nothing carries over to the next refresh.
            await ctx.aclose()

    async def _run(self, stations, ctx: RefreshContext) -> RefreshReport:
        rejections: list[EvidenceRejection] = []

        # 1. Discovery — find candidate sources, never prices.
        with ctx.trace.stage("discovery"):
            candidates = await self._discovery.discover(stations, ctx)

        # 2. Collection — every provider is an independent witness.
        with ctx.trace.stage("collection"):
            collection = await self._collector.collect(candidates, ctx)

        # 2b. Adjustment ledger — record newly announced official price
        # adjustments (fuels the Derivation Engine when evidence is stale).
        adjustments_recorded = 0
        with ctx.trace.stage("adjustments"):
            for provider in self._adjustment_providers:
                try:
                    found = await provider.fetch_adjustments(ctx)
                    adjustments_recorded += await asyncio.to_thread(
                        self._store.record_adjustments, found
                    )
                except Exception:
                    log.exception(
                        "Adjustment provider %s failed", provider.name
                    )

        # 3. Normalization — clean or reject every claim.
        with ctx.trace.stage("normalization"):
            normalized: list[NormalizedEvidence] = []
            for raw in collection.evidence:
                outcome = self._normalizer.normalize(raw)
                if isinstance(outcome, EvidenceRejection):
                    rejections.append(outcome)
                else:
                    normalized.append(outcome)

        # 4. Station matching — resolve identity or reject as ambiguous.
        with ctx.trace.stage("matching"):
            matching = self._matcher_for(stations).match_all(normalized)
            rejections.extend(matching.rejections)

        # 5. Verification — challenge evidence, build agreement clusters.
        with ctx.trace.stage("verification"):
            verification = self._verifier.verify(matching.matched, ctx)
            rejections.extend(verification.rejections)

        # Build the official adjustment-record view from pre-refresh store
        # state: last verified prices + announced deltas predict what each
        # price should be today.
        ctx.ledger_view = await asyncio.to_thread(self._build_ledger_view, stations)

        # 6 & 7. Confidence + conflict resolution per (station, fuel).
        results: list[ResolvedPrice] = []
        with ctx.trace.stage("resolution"):
            for key in self._all_station_fuel_pairs(stations, verification.assessments):
                assessment = verification.assessments.get(key)
                if assessment is None:
                    results.append(
                        self._resolver.resolve(
                            _empty_assessment(*key), {}, ctx
                        )
                    )
                    continue
                confidences = {
                    idx: self._confidence.score(
                        cluster, ctx, station_id=key[0], fuel_type=key[1]
                    )
                    for idx, cluster in enumerate(assessment.clusters)
                }
                for idx, conf in confidences.items():
                    ctx.trace.record(
                        "confidence", "scored",
                        station_id=key[0], fuel_type=key[1].value,
                        price=assessment.clusters[idx].price,
                        score=conf.score, level=conf.level.value,
                        factors=conf.factors,
                    )
                results.append(self._resolver.resolve(assessment, confidences, ctx))

        # 8. Store — with stale-data protection, one transaction.
        with ctx.trace.stage("store"):
            store_actions = await asyncio.to_thread(
                self._store.apply_resolutions, results, ctx.run_id
            )

        # 9. Derivation — advance stale baselines with the official
        # adjustment ledger (clearly labeled DERIVED, never verified).
        derived_count = 0
        if self._derivation is not None:
            with ctx.trace.stage("derivation"):
                derived_count = await asyncio.to_thread(
                    self._derivation.derive_stale, ctx
                )

        await asyncio.to_thread(
            self._update_provider_reliability, matching.matched, results, collection
        )

        finished_at = datetime.now(timezone.utc)
        verified_count = sum(
            1 for r in results if r.status == ResolutionStatus.VERIFIED
        )
        report = RefreshReport(
            run_id=ctx.run_id,
            started_at=ctx.started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            stations_processed=len(stations),
            results=results,
            store_actions=store_actions,
            stats={
                "candidate_sources": len(candidates),
                "evidence_collected": len(collection.evidence),
                "evidence_accepted": len(matching.matched) - len(verification.rejections),
                "evidence_rejected": len(rejections),
                "provider_errors": len(collection.errors),
                "verified": verified_count,
                "unavailable": len(results) - verified_count,
                "adjustments_recorded": adjustments_recorded,
                "derived": derived_count,
                "cache_hits": ctx.cache.hits,
            },
            provider_errors=[
                {"provider": e.provider, "url": e.candidate_url, "error": e.error}
                for e in collection.errors
            ],
            trace=ctx.trace.snapshot() if ctx.trace.enabled else None,
        )
        log.info(
            "Refresh %s finished: %d verified, %d unavailable, %d evidence "
            "rejected, %d provider errors",
            ctx.run_id, verified_count, len(results) - verified_count,
            len(rejections), len(collection.errors),
        )
        return report

    def _build_ledger_view(self, stations) -> LedgerView:
        from datetime import datetime

        priors: dict[tuple[str, str], tuple[float, datetime]] = {}
        for row in self._store.get_all():
            if row.price is None or row.evidence_timestamp is None:
                continue
            if row.status.value not in ("verified", "stale_verified"):
                continue
            priors[(row.station_id, row.fuel_type)] = (
                row.price,
                datetime.fromisoformat(row.evidence_timestamp),
            )
        return LedgerView(
            settings=self._settings,
            stations=stations,
            priors=priors,
            adjustments=self._store.get_adjustments(),
        )

    def _matcher_for(self, stations) -> StationMatchingEngine:
        # Matching is rebuilt per refresh so a station-database edit takes
        # effect on the next investigation.
        return StationMatchingEngine(self._settings, stations)

    @staticmethod
    def _all_station_fuel_pairs(stations, assessments):
        """Every (station, fuel) with evidence, plus every pair already
        assessed — order-stable for reproducible reports."""
        keys = list(assessments.keys())
        keys.sort(key=lambda k: (k[0], k[1].value))
        return keys

    def _update_provider_reliability(
        self, matched: list[NormalizedEvidence], results: list[ResolvedPrice],
        collection,
    ) -> None:
        """Score each witness against the verified outcome.

        A provider whose evidence matches the final verified price earns
        agreement; one that contradicted it earns disagreement. This feeds
        the historical-reliability factor of future refreshes.
        """
        verified: dict[tuple[str, str], float] = {
            (r.station_id, r.fuel_type.value): r.price
            for r in results
            if r.status == ResolutionStatus.VERIFIED and r.price is not None
        }
        agreements: dict[str, int] = {}
        disagreements: dict[str, int] = {}
        errors: dict[str, int] = {}

        tolerance = self._settings.price_agreement_tolerance
        for item in matched:
            if item.station_id is None:
                continue
            key = (item.station_id, item.fuel_type.value)
            if key not in verified:
                continue
            if abs(item.price - verified[key]) <= tolerance:
                agreements[item.provider_name] = agreements.get(item.provider_name, 0) + 1
            else:
                disagreements[item.provider_name] = (
                    disagreements.get(item.provider_name, 0) + 1
                )
        for error in collection.errors:
            errors[error.provider] = errors.get(error.provider, 0) + 1

        if agreements or disagreements or errors:
            self._store.record_provider_outcomes(agreements, disagreements, errors)


def _empty_assessment(station_id: str, fuel_type: FuelType):
    from fie.models.verification import StationFuelAssessment

    return StationFuelAssessment(station_id=station_id, fuel_type=fuel_type)
