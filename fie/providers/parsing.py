"""Shared fuel-price extraction from free text.

One extraction rule set, used by every text-based provider (web pages,
social posts, OCR output), so parsing behavior is consistent and testable
in a single place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from fie.models.enums import FuelType
from fie.normalization.vocab import find_fuel_mentions

# Pump prices are displayed with decimals (e.g. 62.85). Requiring a decimal
# part avoids misreading octane numbers ("RON 95") as prices.
_PRICE_TOKEN_RE = re.compile(r"(?:₱|php|p)?\s*(\d{2,3}[.,]\d{1,2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedPrice:
    fuel_label: str
    fuel_type: FuelType
    price_text: str
    line: str


def html_to_lines(html: str) -> list[str]:
    """Flatten HTML to text lines, keeping table cells on one line."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for row in soup.find_all("tr"):
        row.insert_after(soup.new_string("\n"))
        for cell in row.find_all(["td", "th"]):
            cell.insert_after(soup.new_string(" | "))
    text = soup.get_text(separator="\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_fuel_prices(lines: list[str]) -> list[ExtractedPrice]:
    """Pair fuel labels with the nearest price token on the same line.

    Deliberately conservative: no fuel label on the line, no extraction.
    A missed price is recoverable on the next refresh; a wrong pairing
    poisons the evidence pool.
    """
    extracted: list[ExtractedPrice] = []
    for line in lines:
        mentions = find_fuel_mentions(line)
        if not mentions:
            continue
        prices = [
            (m.start(), m.group(1)) for m in _PRICE_TOKEN_RE.finditer(line)
        ]
        if not prices:
            continue
        used_price_positions: set[int] = set()
        for fuel_pos, label, fuel_type in mentions:
            best: tuple[int, str] | None = None
            best_distance: int | None = None
            for price_pos, price_text in prices:
                if price_pos in used_price_positions:
                    continue
                distance = abs(price_pos - fuel_pos)
                if best_distance is None or distance < best_distance:
                    best = (price_pos, price_text)
                    best_distance = distance
            if best is None:
                continue
            used_price_positions.add(best[0])
            extracted.append(
                ExtractedPrice(
                    fuel_label=label,
                    fuel_type=fuel_type,
                    price_text=best[1],
                    line=line[:300],
                )
            )
    return extracted
