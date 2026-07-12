"""Per-refresh context: run identity, single-refresh cache, and trace.

A RefreshContext lives for exactly one investigation. Its cache prevents the
same source from being retrieved twice within a refresh and is destroyed when
the refresh completes — every new refresh starts from nothing.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

import httpx

from fie.config import Settings

if TYPE_CHECKING:
    from fie.discovery.models import CandidateSource


class RefreshCache:
    """In-memory cache scoped to a single refresh cycle."""

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        if key in self._entries:
            self.hits += 1
            return self._entries[key]
        self.misses += 1
        return None

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = value

    def destroy(self) -> None:
        self._entries.clear()


class TraceRecorder:
    """Developer Mode trace. When disabled, recording is a no-op."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.events: list[dict[str, Any]] = []
        self.stage_durations_ms: dict[str, float] = {}

    def record(self, stage: str, event: str, **data: Any) -> None:
        if not self.enabled:
            return
        self.events.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "event": event,
                **data,
            }
        )

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000.0
            self.stage_durations_ms[name] = round(
                self.stage_durations_ms.get(name, 0.0) + elapsed, 2
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage_durations_ms": dict(self.stage_durations_ms),
            "events": list(self.events),
        }


class RefreshContext:
    def __init__(
        self,
        settings: Settings,
        developer_mode: bool = False,
        provider_reliability: dict[str, float] | None = None,
    ) -> None:
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc)
        self.settings = settings
        self.cache = RefreshCache()
        self.trace = TraceRecorder(enabled=developer_mode)
        # Historical provider reliability (0..1), loaded from the store.
        self.provider_reliability = provider_reliability or {}
        # Official adjustment-record view (fie.record.LedgerView), set by
        # the orchestrator before confidence scoring.
        self.ledger_view = None
        # Candidates derived mid-collection (e.g. an image found inside a
        # social post that needs OCR). Processed in one bounded extra pass.
        self._derived_candidates: list[CandidateSource] = []
        self._seen_candidate_urls: set[str] = set()
        # One pooled HTTP client per refresh: connections are reused across
        # providers (one TLS handshake per host) and closed with the context.
        self._http_client: httpx.AsyncClient | None = None

    def http_client(self, timeout: float, user_agent: str) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": user_agent},
                limits=httpx.Limits(
                    max_connections=self.settings.max_concurrent_fetches * 2,
                    max_keepalive_connections=self.settings.max_concurrent_fetches,
                ),
            )
        return self._http_client

    def mark_candidate_seen(self, url: str) -> bool:
        """Return True if the URL is new for this refresh."""
        if url in self._seen_candidate_urls:
            return False
        self._seen_candidate_urls.add(url)
        return True

    def add_derived_candidate(self, candidate: "CandidateSource") -> None:
        if self.mark_candidate_seen(candidate.url):
            self._derived_candidates.append(candidate)

    def drain_derived_candidates(self) -> list["CandidateSource"]:
        drained = self._derived_candidates
        self._derived_candidates = []
        return drained

    async def aclose(self) -> None:
        """End of investigation: destroy all per-refresh state."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._http_client = None
        self.cache.destroy()
        self._seen_candidate_urls.clear()
        self._derived_candidates.clear()

    def close(self) -> None:
        """Synchronous variant for non-async callers (no HTTP client)."""
        self.cache.destroy()
        self._seen_candidate_urls.clear()
        self._derived_candidates.clear()
