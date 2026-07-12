"""Models produced by the verification / confidence / resolution engines."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from fie.models.enums import ConfidenceLevel, FuelType, ResolutionStatus
from fie.models.evidence import NormalizedEvidence


class EvidenceCluster(BaseModel):
    """A set of independent evidence items that agree on one price.

    Clusters are the unit the engine reasons about: agreement is measured,
    never averaged — every member claims the same price.
    """

    price: float
    members: list[NormalizedEvidence]
    # Challenge flags set by the Verification Engine (e.g.
    # "population_outlier" when far from the brand's NCR median).
    flags: list[str] = Field(default_factory=list)

    @property
    def has_official_station(self) -> bool:
        return any(
            m.source_type.value == "official_station" for m in self.members
        )

    @property
    def has_official_company(self) -> bool:
        return any(
            m.source_type.value in ("official_company", "official_social")
            for m in self.members
        )

    @property
    def distinct_sources(self) -> int:
        return len({m.source_url for m in self.members})

    @property
    def newest_timestamp(self) -> datetime:
        return max(m.effective_timestamp for m in self.members)


class ClusterConfidence(BaseModel):
    score: float
    level: ConfidenceLevel
    factors: dict[str, float] = Field(default_factory=dict)


class StationFuelAssessment(BaseModel):
    """Verification Engine output for one (station, fuel_type) pair."""

    station_id: str
    fuel_type: FuelType
    clusters: list[EvidenceCluster] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)


class ResolvedPrice(BaseModel):
    """The single surviving answer for one (station, fuel_type) pair."""

    station_id: str
    fuel_type: FuelType
    status: ResolutionStatus
    price: float | None = None
    currency: str = "PHP"
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_score: float = 0.0
    source_name: str | None = None
    source_url: str | None = None
    source_type: str | None = None
    # Newest source/retrieval moment backing the price — used for
    # stale-data protection (never overwrite with older evidence).
    evidence_timestamp: datetime | None = None
    verified_at: datetime | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""
