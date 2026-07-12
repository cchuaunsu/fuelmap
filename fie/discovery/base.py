"""Discovery strategy interface. New strategies plug in without core changes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.station import Station


class DiscoveryStrategy(ABC):
    name: str = "base"

    @abstractmethod
    async def discover(
        self, stations: list[Station], ctx: RefreshContext
    ) -> list[CandidateSource]:
        """Return candidate sources for the given stations. Never prices."""
