"""Official brand-website providers.

One shared retrieval/extraction flow; each brand provider declares its
domains and identity. Site-specific parsing quirks belong in the subclass
(override extract_lines), keeping core logic untouched when a site changes.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.enums import Brand
from fie.models.evidence import RawEvidence
from fie.normalization.timestamps import find_date_mention
from fie.observability import get_logger
from fie.providers.base import BaseProvider, ProviderFetchError
from fie.providers.http import HttpFetcher, HttpFetchError
from fie.providers.parsing import extract_fuel_prices, html_to_lines

log = get_logger("providers.brand")


class BrandWebsiteProvider(BaseProvider):
    """Base for providers that read an official brand web page."""

    name = "brand_website"
    brand: Brand = Brand.UNKNOWN
    domains: tuple[str, ...] = ()

    def can_handle(self, candidate: CandidateSource) -> bool:
        if candidate.requires_ocr:
            return False
        host = urlparse(candidate.url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        try:
            resource = await HttpFetcher(ctx).fetch(candidate.url)
        except HttpFetchError as exc:
            raise ProviderFetchError(str(exc)) from exc

        lines = self.extract_lines(resource.text)
        extracted = extract_fuel_prices(lines)
        page_date = find_date_mention("\n".join(lines))

        evidence = [
            RawEvidence(
                station_candidate=self.station_candidate_from(candidate),
                brand=self.brand.value,
                fuel_type_raw=item.fuel_label,
                price_raw=item.price_text,
                currency_raw="PHP",
                source_name=candidate.source_name,
                source_url=resource.final_url,
                source_type=candidate.source_type,
                provider_name=self.name,
                source_timestamp=page_date,
                confidence_hint=0.8,
                raw_text=item.line,
            )
            for item in extracted
        ]
        log.info(
            "%s: %d price claims from %s", self.name, len(evidence), candidate.url
        )
        return evidence

    def extract_lines(self, html: str) -> list[str]:
        """Override for site-specific structure; default is generic HTML."""
        return html_to_lines(html)


class ShellWebsiteProvider(BrandWebsiteProvider):
    name = "shell_website"
    brand = Brand.SHELL
    domains = ("shell.com.ph", "shell.com")


class PetronWebsiteProvider(BrandWebsiteProvider):
    name = "petron_website"
    brand = Brand.PETRON
    domains = ("petron.com",)


class CaltexWebsiteProvider(BrandWebsiteProvider):
    name = "caltex_website"
    brand = Brand.CALTEX
    domains = ("caltex.com",)


class SeaoilProvider(BrandWebsiteProvider):
    name = "seaoil_website"
    brand = Brand.SEAOIL
    domains = ("seaoil.com.ph",)


class UnioilProvider(BrandWebsiteProvider):
    name = "unioil_website"
    brand = Brand.UNIOIL
    domains = ("unioil.com",)


class CleanfuelProvider(BrandWebsiteProvider):
    name = "cleanfuel_website"
    brand = Brand.CLEANFUEL
    domains = ("cleanfuel.ph",)


class PhoenixProvider(BrandWebsiteProvider):
    name = "phoenix_website"
    brand = Brand.PHOENIX
    domains = ("phoenixfuels.ph",)


ALL_BRAND_WEBSITE_PROVIDERS: tuple[type[BrandWebsiteProvider], ...] = (
    ShellWebsiteProvider,
    PetronWebsiteProvider,
    CaltexWebsiteProvider,
    SeaoilProvider,
    UnioilProvider,
    CleanfuelProvider,
    PhoenixProvider,
)
