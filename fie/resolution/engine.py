"""Conflict Resolution Engine.

When clusters disagree, exactly one survives. Prices are never averaged or
merged. The trust ranking, in order:

1. Contains an official station source
2. Contains an official company source
3. More independently agreeing sources
4. Newest evidence
5. Highest confidence score

If even the winning cluster cannot meet the minimum verifiable score — or
the top contenders are genuinely indistinguishable — the answer is
"Price unavailable". No price beats a wrong price.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fie.config import Settings
from fie.context import RefreshContext
from fie.models.enums import ConfidenceLevel, FuelType, ResolutionStatus
from fie.models.verification import (
    ClusterConfidence,
    EvidenceCluster,
    ResolvedPrice,
    StationFuelAssessment,
)
from fie.observability import get_logger

log = get_logger("resolution")

_STAGE = "resolution"


class ConflictResolutionEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(
        self,
        assessment: StationFuelAssessment,
        confidences: dict[int, ClusterConfidence],
        ctx: RefreshContext,
    ) -> ResolvedPrice:
        """Pick the single surviving price for one (station, fuel) pair.

        `confidences` maps cluster index -> ClusterConfidence.
        """
        station_id = assessment.station_id
        fuel_type = assessment.fuel_type

        if not assessment.clusters:
            return self._unavailable(
                station_id, fuel_type, "no admissible evidence"
            )

        ranked = sorted(
            enumerate(assessment.clusters),
            key=lambda pair: self._rank_key(pair[1], confidences[pair[0]]),
            reverse=True,
        )
        winner_idx, winner = ranked[0]
        winner_confidence = confidences[winner_idx]

        ctx.trace.record(
            _STAGE, "ranked",
            station_id=station_id, fuel_type=fuel_type.value,
            ranking=[
                {
                    "price": cluster.price,
                    "rank_key": self._rank_key(cluster, confidences[idx]),
                    "confidence": confidences[idx].score,
                }
                for idx, cluster in ranked
            ],
        )

        if winner_confidence.score < self._settings.min_verifiable_score:
            return self._unavailable(
                station_id, fuel_type,
                f"strongest evidence scored {winner_confidence.score:.2f}, "
                f"below verifiable minimum {self._settings.min_verifiable_score}",
            )

        if len(ranked) > 1:
            runner_idx, runner = ranked[1]
            if self._indistinguishable(
                winner, winner_confidence, runner, confidences[runner_idx]
            ):
                # Two different prices with identical standing: choosing one
                # would be a guess.
                return self._unavailable(
                    station_id, fuel_type,
                    f"conflicting prices {winner.price:.2f} and "
                    f"{runner.price:.2f} are equally supported",
                )

        best_member = max(
            winner.members, key=lambda m: (m.is_official, m.effective_timestamp)
        )
        return ResolvedPrice(
            station_id=station_id,
            fuel_type=fuel_type,
            status=ResolutionStatus.VERIFIED,
            price=winner.price,
            confidence_level=winner_confidence.level,
            confidence_score=winner_confidence.score,
            source_name=best_member.source_name,
            source_url=best_member.source_url,
            source_type=best_member.source_type.value,
            evidence_timestamp=winner.newest_timestamp,
            verified_at=datetime.now(timezone.utc),
            supporting_evidence_ids=[m.evidence_id for m in winner.members],
        )

    @staticmethod
    def _rank_key(
        cluster: EvidenceCluster, confidence: ClusterConfidence
    ) -> tuple[bool, bool, int, float, float]:
        return (
            cluster.has_official_station,
            cluster.has_official_company,
            cluster.distinct_sources,
            cluster.newest_timestamp.timestamp(),
            confidence.score,
        )

    @staticmethod
    def _indistinguishable(
        a: EvidenceCluster,
        ca: ClusterConfidence,
        b: EvidenceCluster,
        cb: ClusterConfidence,
    ) -> bool:
        return (
            a.has_official_station == b.has_official_station
            and a.has_official_company == b.has_official_company
            and a.distinct_sources == b.distinct_sources
            and abs(
                a.newest_timestamp.timestamp() - b.newest_timestamp.timestamp()
            ) < 60.0
            and abs(ca.score - cb.score) < 0.01
        )

    @staticmethod
    def _unavailable(
        station_id: str, fuel_type: FuelType, reason: str
    ) -> ResolvedPrice:
        log.info(
            "Price unavailable for %s/%s: %s", station_id, fuel_type.value, reason
        )
        return ResolvedPrice(
            station_id=station_id,
            fuel_type=fuel_type,
            status=ResolutionStatus.UNAVAILABLE,
            confidence_level=ConfidenceLevel.LOW,
            confidence_score=0.0,
            reason=reason,
        )
