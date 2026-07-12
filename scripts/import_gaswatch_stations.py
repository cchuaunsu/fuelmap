"""Bootstrap the canonical station database from GasWatch PH city pages.

GasWatch (gaswatchph.com) publishes a per-station dataset for every NCR
city (station name, brand, coordinates). This importer builds/refreshes
fie/data/stations.json from it. Station identity stays canonical in our
database — this is a directory import, not price collection.

Usage:
    .venv/bin/python scripts/import_gaswatch_stations.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fie" / "data" / "stations.json"

NCR_CITY_SLUGS = [
    "quezon-city", "manila", "caloocan", "makati", "pasig", "taguig",
    "paranaque", "las-pinas", "valenzuela", "marikina", "san-juan",
    "mandaluyong", "muntinlupa", "malabon", "pasay", "navotas", "pateros",
]

KNOWN_BRANDS = {
    "shell", "petron", "caltex", "seaoil", "unioil", "cleanfuel",
    "phoenix", "total", "jetti", "ptt",
}
BRAND_MAP = {"flyingv": "flying_v"}

STATIONS_RE = re.compile(r"var\s+STATIONS\s*=\s*(\[)", re.DOTALL)


def extract_stations(html: str) -> list[dict]:
    match = STATIONS_RE.search(html)
    if not match:
        return []
    start = match.start(1)
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return []


def main() -> None:
    stations: dict[str, dict] = {}
    brands = Counter()

    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "FuelIntelligenceEngine/0.2 (personal use; station directory import)"},
    ) as client:
        for slug in NCR_CITY_SLUGS:
            url = f"https://gaswatchph.com/{slug}"
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  ! {slug}: fetch failed ({exc}) — skipping city")
                continue
            entries = extract_stations(response.text)
            fresh = 0
            for entry in entries:
                station_id = f"gw-{entry['id']}"
                if station_id in stations:
                    continue
                raw_brand = str(entry.get("brand", "")).lower()
                brand = BRAND_MAP.get(raw_brand, raw_brand)
                if brand not in KNOWN_BRANDS and brand != "flying_v":
                    brand = "unknown"
                brands[brand] += 1
                stations[station_id] = {
                    "station_id": station_id,
                    "brand": brand,
                    "official_name": entry["name"],
                    "known_aliases": [],
                    "latitude": float(entry["lat"]),
                    "longitude": float(entry["lng"]),
                    "address": entry["name"],
                    "city": entry.get("area", slug.replace("-", " ").title()),
                    "province": "Metro Manila",
                }
                fresh += 1
            print(f"  {slug}: {len(entries)} entries, {fresh} new")
            time.sleep(0.4)  # be polite

    if not stations:
        print("No stations imported; keeping existing stations.json")
        sys.exit(1)

    OUT.write_text(
        json.dumps(
            {
                "_comment": (
                    "Canonical station database, bootstrapped from GasWatch PH "
                    "city pages. Re-run scripts/import_gaswatch_stations.py to "
                    "refresh. Edits (aliases, corrected coordinates) survive "
                    "only if made upstream or re-applied."
                ),
                "stations": sorted(stations.values(), key=lambda s: s["station_id"]),
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {len(stations)} stations to {OUT}")
    print("Brands:", dict(brands.most_common()))


if __name__ == "__main__":
    main()
