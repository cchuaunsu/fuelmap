"""Discovery Engine — runs every registered strategy and dedupes the leads."""

from __future__ import annotations

from fie.context import RefreshContext
from fie.discovery.base import DiscoveryStrategy
from fie.discovery.models import CandidateSource
from fie.models.station import Station
from fie.observability import get_logger

log = get_logger("discovery")

# When two strategies find the same URL, keep the one whose source type
# carries more meaning (official beats unknown).
_SOURCE_TYPE_PRIORITY = {
    "official_station": 0,
    "official_company": 1,
    "official_social": 2,
    "aggregator": 3,
    "business_listing": 4,
    "community": 5,
    "search_index": 6,
    "unknown": 7,
}


class DiscoveryEngine:
    def __init__(self, strategies: list[DiscoveryStrategy]) -> None:
        self._strategies = strategies

    async def discover(
        self, stations: list[Station], ctx: RefreshContext
    ) -> list[CandidateSource]:
        by_url: dict[str, CandidateSource] = {}
        for strategy in self._strategies:
            try:
                found = await strategy.discover(stations, ctx)
            except Exception:
                # One failing strategy never stops discovery.
                log.exception("Discovery strategy %s failed", strategy.name)
                ctx.trace.record(
                    "discovery", "strategy_failed", strategy=strategy.name
                )
                continue
            ctx.trace.record(
                "discovery",
                "strategy_completed",
                strategy=strategy.name,
                candidates=len(found),
            )
            for candidate in found:
                existing = by_url.get(candidate.url)
                if existing is None or (
                    _SOURCE_TYPE_PRIORITY[candidate.source_type.value]
                    < _SOURCE_TYPE_PRIORITY[existing.source_type.value]
                ):
                    by_url[candidate.url] = candidate

        candidates = list(by_url.values())
        for candidate in candidates:
            ctx.mark_candidate_seen(candidate.url)
        log.info(
            "Discovery found %d unique candidate sources via %d strategies",
            len(candidates),
            len(self._strategies),
        )
        return candidates
