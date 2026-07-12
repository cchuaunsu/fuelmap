"""Official Facebook page provider.

Reads recent posts from a brand's or station's official page. Uses the
Graph API when FIE_FACEBOOK_GRAPH_TOKEN is configured; without a token,
public scraping is login-walled and the provider skips honestly rather
than returning junk.

Image-only posts (price board photos) are not parsed here — the provider
emits a derived candidate so the OCR provider can treat the photo as its
own evidence source.
"""

from __future__ import annotations

import httpx

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.evidence import RawEvidence
from fie.normalization.timestamps import parse_timestamp
from fie.observability import get_logger
from fie.providers.base import BaseProvider, ProviderFetchError
from fie.providers.parsing import extract_fuel_prices

log = get_logger("providers.facebook")

_GRAPH_URL = "https://graph.facebook.com/v19.0"
_POST_LIMIT = 10


class FacebookPageProvider(BaseProvider):
    name = "facebook_page"

    def __init__(self, graph_token: str = "") -> None:
        self._token = graph_token

    def can_handle(self, candidate: CandidateSource) -> bool:
        return "facebook.com" in candidate.url and not candidate.requires_ocr

    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        if not self._token:
            log.info(
                "No Facebook Graph token configured; skipping %s "
                "(set FIE_FACEBOOK_GRAPH_TOKEN to enable)",
                candidate.url,
            )
            ctx.trace.record(
                "collection", "provider_skipped",
                provider=self.name, url=candidate.url, reason="no_graph_token",
            )
            return []

        page_id = candidate.metadata.get("page_id") or self._page_slug(candidate.url)
        posts = await self._fetch_posts(page_id, ctx)

        evidence: list[RawEvidence] = []
        for post in posts:
            message = post.get("message", "")
            created = parse_timestamp(post.get("created_time", ""))
            picture = post.get("full_picture")

            for item in extract_fuel_prices(message.splitlines()):
                evidence.append(
                    RawEvidence(
                        station_candidate=self.station_candidate_from(candidate),
                        brand=candidate.brand.value,
                        fuel_type_raw=item.fuel_label,
                        price_raw=item.price_text,
                        source_name=candidate.source_name,
                        source_url=candidate.url,
                        source_type=candidate.source_type,
                        provider_name=self.name,
                        source_timestamp=created,
                        confidence_hint=0.7,
                        raw_text=item.line,
                        metadata={"post_id": post.get("id", "")},
                    )
                )

            # Price-board photo without parseable text: hand it to OCR as a
            # derived candidate for this same refresh.
            if picture and not message.strip():
                derived = candidate.model_copy(
                    update={
                        "url": picture,
                        "requires_ocr": True,
                        "discovered_by": self.name,
                        "metadata": {
                            **candidate.metadata,
                            "origin_post": post.get("id", ""),
                            "origin_url": candidate.url,
                            "source_timestamp": post.get("created_time", ""),
                        },
                    }
                )
                ctx.add_derived_candidate(derived)
                ctx.trace.record(
                    "collection", "derived_candidate",
                    provider=self.name, image_url=picture,
                )
        return evidence

    async def _fetch_posts(self, page_id: str, ctx: RefreshContext) -> list[dict]:
        cache_key = f"fb:{page_id}"
        cached = ctx.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=ctx.settings.http_timeout_s) as client:
                response = await client.get(
                    f"{_GRAPH_URL}/{page_id}/posts",
                    params={
                        "fields": "message,created_time,full_picture",
                        "limit": _POST_LIMIT,
                        "access_token": self._token,
                    },
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderFetchError(f"Facebook Graph fetch failed: {exc}") from exc
        posts = response.json().get("data", [])
        ctx.cache.set(cache_key, posts)
        return posts

    @staticmethod
    def _page_slug(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1]
