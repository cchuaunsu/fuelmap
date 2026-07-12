"""Built-in discovery strategies.

Each strategy is independent and optional. Strategies that depend on an
external service (web search) quietly no-op when the service is not
configured, so the engine keeps working with whatever is available.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from fie.context import RefreshContext
from fie.discovery.base import DiscoveryStrategy
from fie.discovery.models import CandidateSource
from fie.models.enums import Brand, EvidenceScope, SourceType
from fie.models.station import Station
from fie.observability import get_logger

log = get_logger("discovery.strategies")


class KnownSourceStrategy(DiscoveryStrategy):
    """Emits candidates from the curated known-sources registry.

    Covers official company pages, official social pages, per-station pages,
    and business listings. Adding a source is a data change, not a code
    change.
    """

    name = "known_sources"

    def __init__(self, registry_path: Path) -> None:
        self._registry = json.loads(registry_path.read_text(encoding="utf-8"))

    async def discover(
        self, stations: list[Station], ctx: RefreshContext
    ) -> list[CandidateSource]:
        candidates: list[CandidateSource] = []
        brands_in_scope = {s.brand for s in stations}

        # Aggregator pages are per-city datasets: fetch only the cities
        # that actually contain stations in this refresh's scope.
        cities_in_scope = {s.city.lower() for s in stations}
        for page in self._registry.get("aggregator_pages", []):
            page_city = str(page.get("city", "")).lower()
            if page_city and page_city not in cities_in_scope:
                continue
            candidates.append(
                CandidateSource(
                    url=page["url"],
                    source_name=page["name"],
                    source_type=SourceType.AGGREGATOR,
                    scope=EvidenceScope.STATION,
                    discovered_by=self.name,
                )
            )

        for brand_key, entry in self._registry.get("brands", {}).items():
            try:
                brand = Brand(brand_key)
            except ValueError:
                log.warning("Unknown brand %r in known_sources registry", brand_key)
                continue
            if brand not in brands_in_scope:
                continue
            for page in entry.get("company_pages", []) + entry.get("social_pages", []):
                candidates.append(
                    CandidateSource(
                        url=page["url"],
                        source_name=page["name"],
                        source_type=SourceType(page.get("source_type", "official_company")),
                        brand=brand,
                        scope=EvidenceScope.BRAND_REGION,
                        region_hint=[r.lower() for r in page.get("region", [])],
                        requires_ocr=bool(page.get("requires_ocr", False)),
                        discovered_by=self.name,
                    )
                )

        station_ids = {s.station_id: s for s in stations}
        for station_id, pages in self._registry.get("stations", {}).items():
            station = station_ids.get(station_id)
            if station is None:
                continue
            for page in pages:
                candidates.append(
                    CandidateSource(
                        url=page["url"],
                        source_name=page["name"],
                        source_type=SourceType(page.get("source_type", "official_station")),
                        brand=station.brand,
                        station_id_hint=station_id,
                        scope=EvidenceScope.STATION,
                        requires_ocr=bool(page.get("requires_ocr", False)),
                        discovered_by=self.name,
                    )
                )

        for listing in self._registry.get("business_listings", []):
            candidates.append(
                CandidateSource(
                    url=listing["url"],
                    source_name=listing["name"],
                    source_type=SourceType.BUSINESS_LISTING,
                    brand=Brand(listing.get("brand", "unknown")),
                    station_id_hint=listing.get("station_id"),
                    scope=EvidenceScope.STATION,
                    discovered_by=self.name,
                )
            )
        return candidates


class WebSearchStrategy(DiscoveryStrategy):
    """Discovers indexed pages through a configured search backend.

    Supports Serper (Google results API) or a self-hosted SearxNG instance.
    Without configuration it contributes nothing and says so once.
    """

    name = "web_search"
    _RESULTS_PER_STATION = 5

    def __init__(self, serper_api_key: str = "", searx_url: str = "") -> None:
        self._serper_key = serper_api_key
        self._searx_url = searx_url.rstrip("/")

    async def discover(
        self, stations: list[Station], ctx: RefreshContext
    ) -> list[CandidateSource]:
        if not self._serper_key and not self._searx_url:
            log.info("Web search not configured (FIE_SERPER_API_KEY / FIE_SEARX_URL); skipping")
            return []

        candidates: list[CandidateSource] = []
        timeout = ctx.settings.http_timeout_s
        async with httpx.AsyncClient(timeout=timeout) as client:
            for station in stations:
                query = (
                    f'"{station.brand.value}" "{station.official_name}" '
                    f"fuel price today {station.city}"
                )
                try:
                    results = await self._search(client, query)
                except Exception:
                    log.exception("Web search failed for %s", station.station_id)
                    continue
                for result in results[: self._RESULTS_PER_STATION]:
                    candidates.append(
                        CandidateSource(
                            url=result["url"],
                            source_name=result.get("title", result["url"]),
                            source_type=SourceType.SEARCH_INDEX,
                            brand=station.brand,
                            station_id_hint=station.station_id,
                            scope=EvidenceScope.STATION,
                            discovered_by=self.name,
                            metadata={"query": query},
                        )
                    )
        return candidates

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        if self._serper_key:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self._serper_key},
                json={"q": query, "num": self._RESULTS_PER_STATION},
            )
            response.raise_for_status()
            return [
                {"url": item["link"], "title": item.get("title", "")}
                for item in response.json().get("organic", [])
            ]
        response = await client.get(
            f"{self._searx_url}/search",
            params={"q": query, "format": "json"},
        )
        response.raise_for_status()
        return [
            {"url": item["url"], "title": item.get("title", "")}
            for item in response.json().get("results", [])
        ]
