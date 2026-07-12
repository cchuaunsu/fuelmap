"""API response schemas — the engine's public face."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

PRICE_UNAVAILABLE = "Price unavailable"


class StationOut(BaseModel):
    station_id: str
    brand: str
    official_name: str
    known_aliases: list[str]
    latitude: float
    longitude: float
    address: str
    city: str
    province: str


class VerifiedPriceOut(BaseModel):
    station_id: str
    brand: str
    station_name: str
    latitude: float
    longitude: float
    fuel_type: str
    # null price + display "Price unavailable" when nothing is verifiable.
    verified_price: float | None
    currency: str = "PHP"
    display: str
    status: str
    confidence: str
    confidence_score: float
    verification_timestamp: str | None
    last_refresh_timestamp: str
    source_used: str | None
    source_url: str | None
    unavailable_reason: str = ""
    # For DERIVED prices: the transparent arithmetic behind the value,
    # e.g. "₱62.85 verified 2026-07-05 +3.30 eff 2026-07-07".
    derivation_note: str = ""


class StationPricesOut(BaseModel):
    station: StationOut
    prices: list[VerifiedPriceOut]


class RefreshRequest(BaseModel):
    station_ids: list[str] | None = None
    developer_mode: bool = False


class RefreshResponse(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    stations_processed: int
    stats: dict[str, int]
    results: list[VerifiedPriceOut]
    provider_errors: list[dict[str, str]] = Field(default_factory=list)
    # Present only when Developer Mode is enabled and requested.
    developer: dict[str, Any] | None = None
