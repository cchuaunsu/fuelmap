"""Generic providers: business listings, search-discovered pages, and the
catch-all web page fallback. These carry lower confidence hints — they are
witnesses of unknown reliability, and the Confidence Engine treats them so.
"""

from __future__ import annotations

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.enums import SourceType
from fie.models.evidence import RawEvidence
from fie.normalization.timestamps import find_date_mention
from fie.observability import get_logger
from fie.providers.base import BaseProvider, ProviderFetchError
from fie.providers.http import HttpFetcher, HttpFetchError
from fie.providers.parsing import extract_fuel_prices, html_to_lines

log = get_logger("providers.generic")


class _TextPageProvider(BaseProvider):
    """Shared fetch-and-extract flow for text web sources."""

    confidence_hint = 0.4

    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        try:
            resource = await HttpFetcher(ctx).fetch(candidate.url)
        except HttpFetchError as exc:
            raise ProviderFetchError(str(exc)) from exc

        if "text" not in resource.content_type and "html" not in resource.content_type:
            return []

        lines = html_to_lines(resource.text)
        page_date = find_date_mention("\n".join(lines))
        return [
            RawEvidence(
                station_candidate=self.station_candidate_from(candidate),
                brand=candidate.brand.value,
                fuel_type_raw=item.fuel_label,
                price_raw=item.price_text,
                source_name=candidate.source_name,
                source_url=resource.final_url,
                source_type=candidate.source_type,
                provider_name=self.name,
                source_timestamp=page_date,
                confidence_hint=self.confidence_hint,
                raw_text=item.line,
            )
            for item in extract_fuel_prices(lines)
        ]


class BusinessListingProvider(_TextPageProvider):
    name = "business_listing"
    confidence_hint = 0.45

    def can_handle(self, candidate: CandidateSource) -> bool:
        return (
            candidate.source_type == SourceType.BUSINESS_LISTING
            and not candidate.requires_ocr
        )


class GoogleDiscoveryProvider(_TextPageProvider):
    """Retrieves pages surfaced by the web-search discovery strategy."""

    name = "google_discovery"
    confidence_hint = 0.35

    def can_handle(self, candidate: CandidateSource) -> bool:
        return (
            candidate.source_type == SourceType.SEARCH_INDEX
            and not candidate.requires_ocr
        )


class GenericWebPageProvider(_TextPageProvider):
    """Last-resort fallback for any http(s) candidate nothing else claims."""

    name = "generic_web_page"
    confidence_hint = 0.3

    def can_handle(self, candidate: CandidateSource) -> bool:
        return candidate.url.startswith(("http://", "https://")) and not (
            candidate.requires_ocr
        )
