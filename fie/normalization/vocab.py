"""Vocabulary maps: raw labels -> canonical enums.

These are data, deliberately kept editable in one place. Brand product names
map to their RON class; extend the lists as new products appear.
"""

from __future__ import annotations

import re

from fie.models.enums import Brand, FuelType

# Order matters: longer/more specific labels are matched first.
_FUEL_ALIASES: list[tuple[str, FuelType]] = [
    # Brand product names (configurable — verify against current lineups)
    ("blaze 100", FuelType.GASOLINE_100),
    ("xcs", FuelType.GASOLINE_95),
    ("xtra advance", FuelType.GASOLINE_91),
    ("turbo diesel", FuelType.PREMIUM_DIESEL),
    ("diesel max", FuelType.DIESEL),
    ("v-power racing", FuelType.GASOLINE_97),
    ("v power racing", FuelType.GASOLINE_97),
    ("v-power gasoline", FuelType.GASOLINE_95),
    ("v-power diesel", FuelType.PREMIUM_DIESEL),
    ("v power diesel", FuelType.PREMIUM_DIESEL),
    ("fuelsave unleaded", FuelType.GASOLINE_91),
    ("fuelsave diesel", FuelType.DIESEL),
    ("platinum with techron", FuelType.GASOLINE_97),
    ("silver with techron", FuelType.GASOLINE_95),
    ("power diesel", FuelType.PREMIUM_DIESEL),
    ("extreme 97", FuelType.GASOLINE_97),
    ("extreme 95", FuelType.GASOLINE_95),
    ("extreme u", FuelType.GASOLINE_91),
    # Generic labels
    ("ron 100", FuelType.GASOLINE_100),
    ("ron100", FuelType.GASOLINE_100),
    ("ron 97", FuelType.GASOLINE_97),
    ("ron97", FuelType.GASOLINE_97),
    ("ron 95", FuelType.GASOLINE_95),
    ("ron95", FuelType.GASOLINE_95),
    ("ron 91", FuelType.GASOLINE_91),
    ("ron91", FuelType.GASOLINE_91),
    ("premium gasoline", FuelType.GASOLINE_97),
    ("premium plus", FuelType.GASOLINE_97),
    ("premium diesel", FuelType.PREMIUM_DIESEL),
    ("regular gasoline", FuelType.GASOLINE_91),
    ("unleaded", FuelType.GASOLINE_91),
    ("gasoline", FuelType.GASOLINE_95),
    ("gas 95", FuelType.GASOLINE_95),
    ("diesel", FuelType.DIESEL),
    ("kerosene", FuelType.KEROSENE),
    ("gaas", FuelType.KEROSENE),
]

_BRAND_ALIASES: dict[str, Brand] = {
    "shell": Brand.SHELL,
    "pilipinas shell": Brand.SHELL,
    "petron": Brand.PETRON,
    "caltex": Brand.CALTEX,
    "chevron": Brand.CALTEX,
    "seaoil": Brand.SEAOIL,
    "sea oil": Brand.SEAOIL,
    "unioil": Brand.UNIOIL,
    "cleanfuel": Brand.CLEANFUEL,
    "clean fuel": Brand.CLEANFUEL,
    "phoenix": Brand.PHOENIX,
    "flying v": Brand.FLYING_V,
    "flyingv": Brand.FLYING_V,
    "total": Brand.TOTAL,
    "totalenergies": Brand.TOTAL,
    "jetti": Brand.JETTI,
    "ptt": Brand.PTT,
}


def parse_fuel_label(raw: str) -> FuelType | None:
    """Map a raw fuel label to a canonical FuelType, or None if unknown."""
    text = re.sub(r"\s+", " ", raw.lower()).strip()
    for alias, fuel in _FUEL_ALIASES:
        if alias in text:
            return fuel
    return None


def find_fuel_mentions(line: str) -> list[tuple[int, str, FuelType]]:
    """Find every fuel label mentioned in a text line.

    Returns (position, matched_label, fuel_type), longest labels first so
    "premium diesel" wins over "diesel" on the same span.
    """
    text = line.lower()
    mentions: list[tuple[int, str, FuelType]] = []
    claimed: list[tuple[int, int]] = []
    for alias, fuel in _FUEL_ALIASES:
        start = 0
        while True:
            idx = text.find(alias, start)
            if idx == -1:
                break
            span = (idx, idx + len(alias))
            if not any(s < span[1] and span[0] < e for s, e in claimed):
                mentions.append((idx, alias, fuel))
                claimed.append(span)
            start = idx + 1
    mentions.sort(key=lambda m: m[0])
    return mentions


def parse_brand(raw: str | None) -> Brand:
    if not raw:
        return Brand.UNKNOWN
    text = raw.lower().strip()
    if text in _BRAND_ALIASES:
        return _BRAND_ALIASES[text]
    for alias, brand in _BRAND_ALIASES.items():
        if alias in text:
            return brand
    try:
        return Brand(text)
    except ValueError:
        return Brand.UNKNOWN
