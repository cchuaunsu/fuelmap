"""Candidate sources — discovery output, provider input.

A CandidateSource is only a lead: "this URL may contain current fuel
prices". Discovery never determines prices.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from fie.models.enums import Brand, EvidenceScope, SourceType


class CandidateSource(BaseModel):
    candidate_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    url: str
    source_name: str
    source_type: SourceType = SourceType.UNKNOWN
    brand: Brand = Brand.UNKNOWN
    # Set when discovery already associates the source with one station.
    station_id_hint: str | None = None
    scope: EvidenceScope = EvidenceScope.STATION
    region_hint: list[str] = Field(default_factory=list)
    # True when the source is an image (price board photo) needing OCR.
    requires_ocr: bool = False
    discovered_by: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
