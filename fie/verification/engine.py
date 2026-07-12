"""Verification Engine — the heart of the FIE.

Challenges every piece of matched evidence, discards what fails, and groups
the survivors into agreement clusters per (station, fuel type). It answers:
which witnesses are credible, which agree, which disagree, which are stale.

It does NOT pick the final price — that is the Conflict Resolution Engine's
job, informed by the Confidence Engine's scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fie.config import Settings
from fie.context import RefreshContext
from fie.models.enums import FuelType, RejectionReason
from fie.models.evidence import EvidenceRejection, NormalizedEvidence
from fie.models.verification import EvidenceCluster, StationFuelAssessment
from fie.observability import get_logger

log = get_logger("verification")

_STAGE = "verification"


@dataclass
class VerificationResult:
    assessments: dict[tuple[str, FuelType], StationFuelAssessment] = field(
        default_factory=dict
    )
    rejections: list[EvidenceRejection] = field(default_factory=list)


class VerificationEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def allowed_age(self, item: NormalizedEvidence) -> timedelta:
        """Per-evidence freshness allowance.

        Weekly-cadence sources (DOE advisory cycle) declare a longer
        allowance via metadata["max_age_hours"]; the global ceiling bounds
        every override so no source can declare itself immortal.
        """
        hours = self._settings.max_evidence_age_hours
        override = item.metadata.get("max_age_hours")
        if override is not None:
            try:
                hours = float(override)
            except (TypeError, ValueError):
                pass
        return timedelta(
            hours=min(hours, self._settings.max_evidence_age_ceiling_hours)
        )

    def verify(
        self, evidence: list[NormalizedEvidence], ctx: RefreshContext
    ) -> VerificationResult:
        result = VerificationResult()
        now = datetime.now(timezone.utc)

        grouped: dict[tuple[str, FuelType], list[NormalizedEvidence]] = {}
        for item in evidence:
            if item.station_id is None:
                continue
            grouped.setdefault((item.station_id, item.fuel_type), []).append(item)

        # First pass: challenge every group and keep what is admitted.
        admitted_by_key: dict[tuple[str, FuelType], list[NormalizedEvidence]] = {}
        for key, items in grouped.items():
            admitted_by_key[key] = self._challenge(items, now, ctx, result)

        # Population view: brand-level price medians across the region,
        # used to challenge station prices that stand far outside their
        # own brand's pricing.
        medians = self._brand_medians(admitted_by_key)
        price_modes = self._brand_price_modes(admitted_by_key)

        for (station_id, fuel_type), items in grouped.items():
            admitted = admitted_by_key[(station_id, fuel_type)]
            clusters = self._cluster(admitted)
            self._flag_population_outliers(
                clusters, fuel_type, medians, price_modes
            )
            result.assessments[(station_id, fuel_type)] = StationFuelAssessment(
                station_id=station_id,
                fuel_type=fuel_type,
                clusters=clusters,
            )
            ctx.trace.record(
                _STAGE, "assessed",
                station_id=station_id, fuel_type=fuel_type.value,
                evidence_in=len(items), admitted=len(admitted),
                clusters=[
                    {
                        "price": c.price,
                        "members": len(c.members),
                        "distinct_sources": c.distinct_sources,
                        "official_station": c.has_official_station,
                        "official_company": c.has_official_company,
                    }
                    for c in clusters
                ],
            )
        return result

    def _challenge(
        self,
        items: list[NormalizedEvidence],
        now: datetime,
        ctx: RefreshContext,
        result: VerificationResult,
    ) -> list[NormalizedEvidence]:
        """Interrogate each witness. Only credible, current, non-duplicate
        evidence is admitted."""
        admitted: list[NormalizedEvidence] = []
        seen: set[tuple[str, float]] = set()

        for item in items:
            age = now - item.effective_timestamp
            max_age = self.allowed_age(item)
            if age > max_age:
                result.rejections.append(self._reject(
                    item, RejectionReason.OUTDATED,
                    f"evidence is {age.total_seconds() / 3600:.1f}h old "
                    f"(max {max_age.total_seconds() / 3600:.0f}h)",
                ))
                continue

            reliability = ctx.provider_reliability.get(item.provider_name)
            if (
                reliability is not None
                and reliability < self._settings.min_provider_reliability
            ):
                result.rejections.append(self._reject(
                    item, RejectionReason.UNRELIABLE_PROVIDER,
                    f"historical reliability {reliability:.2f} below "
                    f"{self._settings.min_provider_reliability}",
                ))
                continue

            # The same URL asserting the same price twice is one witness,
            # not two.
            key = (item.source_url, item.price)
            if key in seen:
                result.rejections.append(self._reject(
                    item, RejectionReason.DUPLICATE,
                    "same source already asserted this price",
                ))
                continue
            seen.add(key)
            admitted.append(item)

        return admitted

    def _brand_medians(
        self,
        admitted_by_key: dict[tuple[str, FuelType], list[NormalizedEvidence]],
    ) -> dict[tuple[str, FuelType], float]:
        """Median admitted price per (brand, fuel) across the region."""
        from statistics import median

        prices: dict[tuple[str, FuelType], list[float]] = {}
        for (_, fuel_type), items in admitted_by_key.items():
            for item in items:
                prices.setdefault((item.brand.value, fuel_type), []).append(
                    item.price
                )
        return {
            key: median(values)
            for key, values in prices.items()
            if len(values) >= self._settings.population_outlier_min_sample
        }

    def _brand_price_modes(
        self,
        admitted_by_key: dict[tuple[str, FuelType], list[NormalizedEvidence]],
    ) -> dict[tuple[str, FuelType, float], int]:
        """How many distinct stations share each exact (brand, fuel, price)."""
        stations: dict[tuple[str, FuelType, float], set] = {}
        for (station_id, fuel_type), items in admitted_by_key.items():
            for item in items:
                key = (item.brand.value, fuel_type, round(item.price, 2))
                stations.setdefault(key, set()).add(station_id)
        return {key: len(ids) for key, ids in stations.items()}

    def _flag_population_outliers(
        self,
        clusters: list[EvidenceCluster],
        fuel_type: FuelType,
        medians: dict[tuple[str, FuelType], float],
        price_modes: dict[tuple[str, FuelType, float], int],
    ) -> None:
        """Challenge prices standing far outside their brand's own pricing.

        The price is not altered or rejected — stations do reprice — but a
        claim >N% away from every same-brand station in the region cannot
        carry HIGH confidence on a single witness.

        Exception: an identical price shared by many same-brand stations
        is a published price point (brands price premium products
        uniformly, which makes some fuels bimodal), not a data defect.
        Only a price that is both far from the brand median AND rare
        stays flagged — a typo is unique; a price list repeats.
        """
        fraction = self._settings.population_outlier_fraction
        min_sample = self._settings.population_outlier_min_sample
        for cluster in clusters:
            brand = cluster.members[0].brand.value
            benchmark = medians.get((brand, fuel_type))
            if benchmark is None or benchmark <= 0:
                continue
            if abs(cluster.price - benchmark) / benchmark <= fraction:
                continue
            shared_by = price_modes.get(
                (brand, fuel_type, round(cluster.price, 2)), 0
            )
            if shared_by >= min_sample:
                continue
            cluster.flags.append("population_outlier")

    def _cluster(
        self, items: list[NormalizedEvidence]
    ) -> list[EvidenceCluster]:
        """Group evidence whose prices agree within tolerance.

        Members of a cluster corroborate each other; separate clusters are
        conflicting claims to be resolved later. Prices are never averaged —
        a cluster's price is the price its most authoritative newest member
        actually stated.
        """
        tolerance = self._settings.price_agreement_tolerance
        clusters: list[list[NormalizedEvidence]] = []
        for item in sorted(items, key=lambda e: e.price):
            placed = False
            for cluster in clusters:
                if abs(cluster[0].price - item.price) <= tolerance:
                    cluster.append(item)
                    placed = True
                    break
            if not placed:
                clusters.append([item])

        built: list[EvidenceCluster] = []
        for members in clusters:
            representative = max(
                members,
                key=lambda e: (e.is_official, e.effective_timestamp),
            )
            built.append(
                EvidenceCluster(price=representative.price, members=members)
            )
        return built

    @staticmethod
    def _reject(
        item: NormalizedEvidence, reason: RejectionReason, detail: str
    ) -> EvidenceRejection:
        log.debug("Verification rejection %s: %s", item.evidence_id, detail)
        return EvidenceRejection(
            evidence_id=item.evidence_id,
            stage=_STAGE,
            reason=reason,
            detail=detail,
            provider_name=item.provider_name,
            source_url=item.source_url,
        )
