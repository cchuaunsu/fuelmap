"""Provider framework base.

A provider is a witness: it retrieves evidence from one kind of source and
returns it in the standard RawEvidence shape. Providers never decide the
final answer and never talk to each other.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.evidence import RawEvidence, StationCandidate


class ProviderFetchError(Exception):
    """Raised when a provider cannot retrieve its source."""


class BaseProvider(ABC):
    """Interface every provider implements.

    New sources plug in by subclassing this and registering the instance —
    no core logic changes.
    """

    name: str = "base"

    @abstractmethod
    def can_handle(self, candidate: CandidateSource) -> bool:
        """Whether this provider knows how to retrieve this candidate."""

    @abstractmethod
    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        """Retrieve the source and return zero or more evidence items.

        Must raise ProviderFetchError (or any exception) on failure; the
        Evidence Collector isolates failures so one provider never stops
        the engine.
        """

    def is_available(self) -> bool:
        """Providers with unmet runtime dependencies (e.g. no OCR backend
        installed) report False and are skipped, not errored."""
        return True

    @staticmethod
    def station_candidate_from(candidate: CandidateSource) -> StationCandidate:
        """Carry the discovery context into the evidence."""
        return StationCandidate(
            brand_hint=candidate.brand.value if candidate.brand.value != "unknown" else None,
            station_id_hint=candidate.station_id_hint,
            scope=candidate.scope,
            region_hint=list(candidate.region_hint),
        )
