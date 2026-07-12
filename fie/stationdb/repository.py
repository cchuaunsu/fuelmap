"""Station Database — the single canonical registry of stations.

The repository is an interface so the JSON-file implementation can be
replaced (e.g. by PostgreSQL) without touching any other module.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from fie.models.enums import Brand
from fie.models.station import Station
from fie.observability import get_logger

log = get_logger("stationdb")


class StationRepository(ABC):
    @abstractmethod
    def get_all(self) -> list[Station]: ...

    @abstractmethod
    def get_by_id(self, station_id: str) -> Station | None: ...

    @abstractmethod
    def get_by_brand(self, brand: Brand) -> list[Station]: ...


class JsonStationRepository(StationRepository):
    """Reads the canonical station list from a JSON file at startup."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stations: dict[str, Station] = {}
        self._load()

    def _load(self) -> None:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        for item in raw["stations"]:
            station = Station.model_validate(item)
            if station.station_id in self._stations:
                raise ValueError(
                    f"Duplicate station_id in station database: {station.station_id}"
                )
            self._stations[station.station_id] = station
        log.info("Loaded %d canonical stations from %s", len(self._stations), self._path)

    def get_all(self) -> list[Station]:
        return list(self._stations.values())

    def get_by_id(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def get_by_brand(self, brand: Brand) -> list[Station]:
        return [s for s in self._stations.values() if s.brand == brand]
