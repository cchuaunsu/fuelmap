"""Adjustment providers — witnesses for the price-adjustment ledger.

Philippine oil companies announce per-liter adjustments every week
(effective Tuesday 6 a.m.), and news outlets publish the exact per-company
figures. These providers parse such announcements into PriceAdjustment
records. They feed the ledger, never prices directly.

Parsing is deliberately conservative: a sentence that cannot be read
unambiguously (one clear direction, clear fuel/amount pairs) contributes
nothing. A missing ledger entry only delays derivation; a wrong delta
would corrupt every derived price built on it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from fie.context import RefreshContext
from fie.models.adjustment import PriceAdjustment
from fie.observability import get_logger
from fie.providers.http import HttpFetcher, HttpFetchError

log = get_logger("providers.adjustments")


class BaseAdjustmentProvider(ABC):
    name: str = "adjustments_base"

    @abstractmethod
    async def fetch_adjustments(
        self, ctx: RefreshContext
    ) -> list[PriceAdjustment]: ...


_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.DOTALL)
_LINK_RE = re.compile(r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", re.DOTALL)

_RELEVANT_TITLE_RE = re.compile(
    r"(pump|fuel|oil)\s+price", re.IGNORECASE
)
_ADJUST_WORDS_RE = re.compile(
    r"adjust|hike|rollback|roll\s*back|cut|increase|reduc", re.IGNORECASE
)
# Forecast pieces ("adjustments expected next week") describe the future,
# not effective adjustments — skip them and keep scanning for the latest
# actual announcement.
_FORECAST_TITLE_RE = re.compile(
    r"expect|forecast|likely|loom|next week|to hike|to cut|to raise|may\s",
    re.IGNORECASE,
)

_COMPANIES = {
    "petron": "petron",
    "shell": "shell",
    "pilipinas shell": "shell",
    "caltex": "caltex",
    "chevron": "caltex",
    "seaoil": "seaoil",
    "phoenix": "phoenix",
    "cleanfuel": "cleanfuel",
    "unioil": "unioil",
    "flying v": "flying_v",
    "ptt": "ptt",
    "total": "total",
    "jetti": "jetti",
}

_UP_WORDS = ("hike", "hiked", "increase", "increased", "raise", "raised", "up by")
_DOWN_WORDS = (
    "rollback", "rolled back", "roll back", "cut", "slashed", "reduce",
    "reduced", "lower", "lowered", "down by",
)

# "... prices per liter of gasoline by P0.25" / "diesel by P3.30 per liter"
_FUEL_AMOUNT_RE = re.compile(
    r"(gasoline|diesel|kerosene)[^.;]{0,60}?by\s+(?:php|p|₱)\s*(\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

_EFFECTIVE_DATE_RE = re.compile(
    r"(?:effective|on)\s+(?:\w+day,?\s+)?"
    r"((?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


class GmaRssAdjustmentProvider(BaseAdjustmentProvider):
    """Finds the latest weekly pump-price adjustment article via GMA News'
    money RSS feed and extracts the per-company deltas."""

    name = "gma_rss_adjustments"

    def __init__(self, feed_url: str) -> None:
        self._feed_url = feed_url

    async def fetch_adjustments(
        self, ctx: RefreshContext
    ) -> list[PriceAdjustment]:
        fetcher = HttpFetcher(ctx)
        try:
            feed = await fetcher.fetch(self._feed_url)
        except HttpFetchError as exc:
            log.warning("Adjustment feed unreachable: %s", exc)
            return []

        article_url = self._find_article(feed.text)
        if article_url is None:
            log.info("No pump-price adjustment article in the feed right now")
            return []

        try:
            article = await fetcher.fetch(article_url)
        except HttpFetchError as exc:
            log.warning("Adjustment article unreachable: %s", exc)
            return []

        adjustments = self._parse_article(article.text, article_url)
        log.info(
            "%s: %d adjustment entries from %s",
            self.name, len(adjustments), article_url,
        )
        ctx.trace.record(
            "adjustments", "parsed",
            provider=self.name, url=article_url, entries=len(adjustments),
        )
        return adjustments

    def _find_article(self, feed_xml: str) -> str | None:
        for item in _ITEM_RE.finditer(feed_xml):
            block = item.group(1)
            title_match = _TITLE_RE.search(block)
            link_match = _LINK_RE.search(block)
            if not title_match or not link_match:
                continue
            title = title_match.group(1)
            if (
                _RELEVANT_TITLE_RE.search(title)
                and _ADJUST_WORDS_RE.search(title)
                and not _FORECAST_TITLE_RE.search(title)
            ):
                return link_match.group(1).strip()
        return None

    def _parse_article(self, html: str, url: str) -> list[PriceAdjustment]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(separator=" "))
        # Corporate abbreviations ("Seaoil Philippines Corp.") would break
        # sentence splitting and sever companies from their deltas.
        text = re.sub(r"\b(Corp|Inc|Co|Ltd|Phils|Jr|Sr)\.", r"\1", text)

        # PH price adjustments always take effect on Tuesdays; among the
        # dates mentioned, prefer one that actually falls on a Tuesday.
        candidates = []
        for match in _EFFECTIVE_DATE_RE.finditer(text):
            raw_date = re.sub(r"\s+", " ", match.group(1)).replace(",", "")
            try:
                candidates.append(datetime.strptime(raw_date, "%B %d %Y").date())
            except ValueError:
                continue
        if not candidates:
            log.info("No effective date found in adjustment article; skipping")
            return []
        tuesdays = [d for d in candidates if d.weekday() == 1]
        effective = tuesdays[0] if tuesdays else candidates[0]

        adjustments: list[PriceAdjustment] = []
        now = datetime.now(timezone.utc)
        for sentence in re.split(r"(?<=[.;])\s+", text):
            lowered = sentence.lower()
            went_up = any(w in lowered for w in _UP_WORDS)
            went_down = any(w in lowered for w in _DOWN_WORDS)
            if went_up == went_down:
                continue  # no direction or contradictory — refuse to guess
            sign = 1.0 if went_up else -1.0

            companies = sorted(
                {slug for alias, slug in _COMPANIES.items() if alias in lowered}
            )
            if not companies:
                if "oil compan" in lowered or "oil firm" in lowered:
                    companies = ["all"]
                else:
                    continue

            for fuel_word, amount in _FUEL_AMOUNT_RE.findall(sentence):
                delta = sign * float(amount)
                if abs(delta) > 15.0:
                    continue  # weekly moves are centavos-to-few-pesos
                for company in companies:
                    adjustments.append(
                        PriceAdjustment(
                            brand=company,
                            fuel_class=fuel_word.lower(),
                            delta=round(delta, 2),
                            effective_date=effective,
                            source_name="GMA News pump price advisory",
                            source_url=url,
                            announced_at=now,
                        )
                    )
        return adjustments
