"""Canonical station model — the single source of station identity."""

from __future__ import annotations

from pydantic import BaseModel, Field

from fie.models.enums import Brand


class Station(BaseModel):
    """One canonical fuel station.

    Every provider's evidence ultimately maps to exactly one Station via the
    Station Matching Engine. Stations are never created from evidence.
    """

    station_id: str
    brand: Brand
    official_name: str
    known_aliases: list[str] = Field(default_factory=list)
    latitude: float
    longitude: float
    address: str
    city: str
    province: str

    def all_names(self) -> list[str]:
        return [self.official_name, *self.known_aliases]
