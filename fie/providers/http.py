"""Shared HTTP retrieval with the per-refresh cache.

All providers fetch through this so no source is retrieved twice within one
refresh cycle. The underlying connection pool lives on the RefreshContext —
one TLS handshake per host per refresh instead of one per request — and
dies with it.

Retries are reserved for transient failures (timeouts, transport errors,
5xx). Permanent answers (4xx, redirect loops) fail immediately.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from fie.context import RefreshContext
from fie.observability import get_logger

log = get_logger("http")


@dataclass(frozen=True)
class FetchedResource:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    content: bytes
    fetched_at: datetime


class HttpFetchError(Exception):
    pass


class HttpFetcher:
    def __init__(self, ctx: RefreshContext) -> None:
        self._ctx = ctx
        self._settings = ctx.settings

    async def fetch(self, url: str) -> FetchedResource:
        cache_key = f"http:{url}"
        cached = self._ctx.cache.get(cache_key)
        if cached is not None:
            return cached

        client = self._ctx.http_client(
            timeout=self._settings.http_timeout_s,
            user_agent=self._settings.user_agent,
        )

        last_error: Exception | None = None
        for attempt in range(self._settings.http_retries + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                resource = FetchedResource(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    text=response.text,
                    content=response.content,
                    fetched_at=datetime.now(timezone.utc),
                )
                self._ctx.cache.set(cache_key, resource)
                return resource
            except httpx.HTTPStatusError as exc:
                # 4xx (and uncollapsed 3xx) are the server's final answer —
                # retrying wastes seconds. Only 5xx may be transient.
                if exc.response.status_code < 500:
                    raise HttpFetchError(f"GET {url} failed: {exc}") from exc
                last_error = exc
            except (httpx.TimeoutException, httpx.TransportError, httpx.InvalidURL) as exc:
                last_error = exc
            if attempt < self._settings.http_retries:
                await asyncio.sleep(0.5 * (attempt + 1))

        raise HttpFetchError(f"GET {url} failed: {last_error}") from last_error
