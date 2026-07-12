"""GasWatch PH aggregator provider.

GasWatch (gaswatchph.com) publishes per-station pump prices for every NCR
city, fed by the DOE weekly retail price advisory plus community reports.
Each city page embeds the dataset as `var STATIONS = [...]` with a
`var LAST_UPDATED = "<date>"` marker.

This is one witness among many: evidence carries source_type AGGREGATOR and
a weekly-cadence age allowance, and is challenged like everything else.
"""

from __future__ import annotations

import json
import re

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.enums import EvidenceScope, SourceType
from fie.models.evidence import RawEvidence, StationCandidate
from fie.normalization.timestamps import parse_timestamp
from fie.observability import get_logger
from fie.providers.base import BaseProvider, ProviderFetchError
from fie.providers.http import HttpFetcher, HttpFetchError

log = get_logger("providers.gaswatch")

# GasWatch price keys -> labels our normalization vocabulary understands.
_FUEL_KEY_LABELS = {
    "diesel": "diesel",
    "premiumDiesel": "premium diesel",
    "unleaded": "ron 91",
    "premium95": "ron 95",
    "premium97": "ron 97",
    "kerosene": "kerosene",
    # "egasoline" intentionally unmapped: its RON class is not stated, and
    # mapping it by guesswork could file a price under the wrong fuel.
}

# The dataset follows the DOE weekly advisory cycle (Tuesdays); allow a
# little over a week before it counts as stale.
_WEEKLY_CADENCE_HOURS = 240.0

_STATIONS_RE = re.compile(r"var\s+STATIONS\s*=\s*(\[)")
_UPDATED_RE = re.compile(r"var\s+LAST_UPDATED\s*=\s*\"([^\"]+)\"")


class GasWatchProvider(BaseProvider):
    name = "gaswatch"

    def can_handle(self, candidate: CandidateSource) -> bool:
        return "gaswatchph.com" in candidate.url and not candidate.requires_ocr

    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        try:
            resource = await HttpFetcher(ctx).fetch(candidate.url)
        except HttpFetchError as exc:
            raise ProviderFetchError(str(exc)) from exc

        stations = self._extract_stations(resource.text)
        if not stations:
            raise ProviderFetchError(
                f"no embedded station dataset found at {candidate.url} "
                "(page structure may have changed)"
            )

        updated_match = _UPDATED_RE.search(resource.text)
        source_timestamp = (
            parse_timestamp(updated_match.group(1)) if updated_match else None
        )

        evidence: list[RawEvidence] = []
        for entry in stations:
            try:
                station_hint = f"gw-{entry['id']}"
                brand = str(entry.get("brand", "")).lower()
                name = str(entry.get("name", ""))
                lat = float(entry["lat"])
                lng = float(entry["lng"])
                prices = entry.get("prices") or {}
            except (KeyError, TypeError, ValueError):
                continue

            for key, label in _FUEL_KEY_LABELS.items():
                price = prices.get(key)
                if price is None:
                    continue
                evidence.append(
                    RawEvidence(
                        station_candidate=StationCandidate(
                            name=name,
                            brand_hint=brand,
                            latitude=lat,
                            longitude=lng,
                            station_id_hint=station_hint,
                            scope=EvidenceScope.STATION,
                        ),
                        brand=brand,
                        fuel_type_raw=label,
                        price_raw=str(price),
                        source_name="GasWatch PH",
                        source_url=candidate.url,
                        source_type=SourceType.AGGREGATOR,
                        provider_name=self.name,
                        source_timestamp=source_timestamp,
                        confidence_hint=0.65,
                        raw_text=f"{name} {label} {price}",
                        metadata={
                            "max_age_hours": _WEEKLY_CADENCE_HOURS,
                            "gaswatch_id": entry.get("id"),
                        },
                    )
                )
        log.info(
            "%s: %d price claims from %s (updated %s)",
            self.name, len(evidence), candidate.url,
            source_timestamp.date() if source_timestamp else "unknown",
        )
        return evidence

    @staticmethod
    def _extract_stations(html: str) -> list[dict]:
        match = _STATIONS_RE.search(html)
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
                    try:
                        return json.loads(html[start : i + 1])
                    except json.JSONDecodeError:
                        return []
        return []
