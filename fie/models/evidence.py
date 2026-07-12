"""The standard Evidence object.

Every provider returns RawEvidence in exactly this shape. Downstream engines
(verification, confidence, resolution) never need to know which provider
produced a piece of evidence — only its declared source characteristics.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from fie.models.enums import (
    Brand,
    EvidenceScope,
    FuelType,
    RejectionReason,
    SourceType,
)


def _new_evidence_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StationCandidate(BaseModel):
    """How the source referred to the station — before matching."""

    name: str | None = None
    brand_hint: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Set when discovery already tied the source to a canonical station.
    # The matching engine validates the hint; it does not blindly trust it.
    station_id_hint: str | None = None
    scope: EvidenceScope = EvidenceScope.STATION
    # For BRAND_REGION claims: the region the source says it covers,
    # e.g. ["metro manila"] or city names. Lowercase free text.
    region_hint: list[str] = Field(default_factory=list)


class RawEvidence(BaseModel):
    """A single price claim exactly as retrieved from a source."""

    evidence_id: str = Field(default_factory=_new_evidence_id)
    station_candidate: StationCandidate
    brand: str | None = None
    fuel_type_raw: str
    price_raw: str
    currency_raw: str = "PHP"
    source_name: str
    source_url: str
    source_type: SourceType
    provider_name: str
    retrieval_timestamp: datetime = Field(default_factory=utcnow)
    source_timestamp: datetime | None = None
    # The provider's own 0..1 hint about this claim's quality (e.g. OCR
    # confidence). A hint only — the Confidence Engine decides what it means.
    confidence_hint: float = 0.5
    raw_text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedEvidence(BaseModel):
    """Evidence after the Normalization Engine has cleaned and validated it."""

    evidence_id: str
    station_candidate: StationCandidate
    # Resolved by the Station Matching Engine; None until matched.
    station_id: str | None = None
    brand: Brand = Brand.UNKNOWN
    fuel_type: FuelType
    price: float                      # PHP per liter
    currency: str = "PHP"
    source_name: str
    source_url: str
    source_type: SourceType
    provider_name: str
    retrieval_timestamp: datetime
    source_timestamp: datetime | None = None
    confidence_hint: float = 0.5
    via_ocr: bool = False
    ocr_confidence: float | None = None
    raw_text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_timestamp(self) -> datetime:
        """Best-known moment the claim was made."""
        return self.source_timestamp or self.retrieval_timestamp

    @property
    def is_official(self) -> bool:
        return self.source_type in (
            SourceType.OFFICIAL_STATION,
            SourceType.OFFICIAL_COMPANY,
            SourceType.OFFICIAL_SOCIAL,
        )


class EvidenceRejection(BaseModel):
    """A challenged piece of evidence that did not survive. Kept for audit."""

    evidence_id: str
    stage: str
    reason: RejectionReason
    detail: str = ""
    provider_name: str | None = None
    source_url: str | None = None
