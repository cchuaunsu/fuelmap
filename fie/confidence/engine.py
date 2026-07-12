"""Confidence Engine.

Scores an evidence cluster on transparent, weighted factors. The factor
breakdown is preserved so Developer Mode can show exactly why a price got
its confidence level.

Announcement-cycle awareness: Philippine pump prices move only through
officially announced weekly adjustments (effective Tuesdays). The engine
exploits this in two ways:

- Recency is judged against the adjustment record, not just the clock —
  evidence is still current if no adjustment has taken effect since it was
  published.
- A price that equals the last verified price plus the announced deltas
  since ("record-consistent") is corroborated by an independent evidence
  chain and counts like an agreeing witness.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fie.config import Settings
from fie.context import RefreshContext
from fie.models.enums import ConfidenceLevel, SourceType
from fie.models.verification import ClusterConfidence, EvidenceCluster

_OFFICIALNESS: dict[SourceType, float] = {
    SourceType.OFFICIAL_STATION: 1.00,
    SourceType.OFFICIAL_COMPANY: 0.85,
    SourceType.OFFICIAL_SOCIAL: 0.75,
    # Public aggregators that republish the DOE weekly advisory plus
    # community reports: more than hearsay, less than the source itself.
    SourceType.AGGREGATOR: 0.60,
    SourceType.BUSINESS_LISTING: 0.45,
    SourceType.COMMUNITY: 0.35,
    SourceType.SEARCH_INDEX: 0.30,
    SourceType.UNKNOWN: 0.25,
}

_WEIGHTS = {
    "officialness": 0.35,
    "recency": 0.25,
    "corroboration": 0.20,
    "provider_reliability": 0.10,
    "timestamped": 0.05,
    "signal_quality": 0.05,
}


class ConfidenceEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def score(
        self,
        cluster: EvidenceCluster,
        ctx: RefreshContext,
        station_id: str | None = None,
        fuel_type=None,
    ) -> ClusterConfidence:
        now = datetime.now(timezone.utc)
        factors: dict[str, float] = {}

        # Officialness of the best source in the cluster; OCR-derived
        # evidence is discounted by its own OCR confidence.
        factors["officialness"] = max(
            _OFFICIALNESS[m.source_type]
            * (m.ocr_confidence if m.via_ocr and m.ocr_confidence else 1.0)
            for m in cluster.members
        )

        factors["recency"] = self._recency(cluster, now, station_id, fuel_type, ctx)

        # Independent corroboration: distinct agreeing sources, plus the
        # adjustment-record chain when it confirms this exact price.
        record = (
            ctx.ledger_view.assess(station_id, fuel_type, cluster.price)
            if ctx.ledger_view is not None and station_id is not None
            else None
        )
        agreeing = cluster.distinct_sources - 1
        if record is not None and record.consistent:
            agreeing += 1
        factors["corroboration"] = min(agreeing, 2) / 2.0

        # Historical reliability of the providers that produced the evidence
        # (Laplace-smoothed in the store; unknown providers get a neutral 0.5).
        reliabilities = [
            ctx.provider_reliability.get(m.provider_name, 0.5)
            for m in cluster.members
        ]
        factors["provider_reliability"] = sum(reliabilities) / len(reliabilities)

        # Claims with a stated source timestamp beat undated ones.
        factors["timestamped"] = (
            1.0 if any(m.source_timestamp for m in cluster.members) else 0.4
        )

        # Providers' own quality hints (OCR confidence, parse certainty).
        factors["signal_quality"] = sum(
            m.confidence_hint for m in cluster.members
        ) / len(cluster.members)

        score = sum(_WEIGHTS[name] * value for name, value in factors.items())
        level = self._level(score)

        # Challenges: a price far from what the official adjustment record
        # predicts, or far outside its own brand's regional pricing, is
        # suspect regardless of its arithmetic score. It may be genuine
        # (stations do reprice), so it is downgraded, not rejected — and
        # the reason is exposed for Developer Mode.
        if record is not None and record.deviates:
            factors["record_deviation_pesos"] = round(record.deviation, 2)
            if level == ConfidenceLevel.HIGH:
                level = ConfidenceLevel.MEDIUM
        if "population_outlier" in cluster.flags:
            factors["population_outlier"] = 1.0
            if level == ConfidenceLevel.HIGH:
                level = ConfidenceLevel.MEDIUM

        return ClusterConfidence(
            score=round(score, 4),
            level=level,
            factors={k: round(v, 4) for k, v in factors.items()},
        )

    def _recency(
        self, cluster: EvidenceCluster, now: datetime, station_id, fuel_type, ctx
    ) -> float:
        age_hours = (now - cluster.newest_timestamp).total_seconds() / 3600.0
        max_age = max(self._allowed_age_hours(m) for m in cluster.members)
        if age_hours <= 6.0:
            return 1.0

        # Announcement-aware: if the adjustment record shows no price change
        # taking effect after this evidence was published, the claim is
        # still current — prices only move through announced adjustments.
        # (Verification's hard age gate still rejects anything beyond the
        # cadence ceiling, so this never resurrects truly old data.)
        if ctx.ledger_view is not None and station_id is not None:
            record = ctx.ledger_view.assess(station_id, fuel_type, cluster.price)
            if (
                record is not None
                and record.ledger_has_data
                and not record.adjusted_since(cluster.newest_timestamp)
            ):
                return 0.95

        return max(0.0, 1.0 - (age_hours - 6.0) / (max_age - 6.0))

    def _allowed_age_hours(self, member) -> float:
        try:
            hours = float(
                member.metadata.get(
                    "max_age_hours", self._settings.max_evidence_age_hours
                )
            )
        except (TypeError, ValueError):
            hours = self._settings.max_evidence_age_hours
        return min(hours, self._settings.max_evidence_age_ceiling_hours)

    def _level(self, score: float) -> ConfidenceLevel:
        if score >= self._settings.high_confidence_threshold:
            return ConfidenceLevel.HIGH
        if score >= self._settings.medium_confidence_threshold:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW
