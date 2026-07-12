"""Station Matching Engine.

Resolves every piece of normalized evidence to canonical stations — or
rejects it. Two claims about "Shell C5", "Shell Libis", and "Shell Eastwood"
must land on the same station; an ambiguous claim lands on none.

Two scopes:
- STATION evidence resolves to exactly one station (or is rejected).
- BRAND_REGION evidence (an official brand-wide claim with stated coverage)
  fans out to every station of that brand inside the stated region. That is
  the source's own claim, not interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from fie.config import Settings
from fie.models.enums import Brand, EvidenceScope, RejectionReason
from fie.models.evidence import EvidenceRejection, NormalizedEvidence
from fie.models.station import Station
from fie.matching.geo import haversine_m
from fie.normalization.text import normalize_text, token_jaccard
from fie.observability import get_logger

log = get_logger("matching")

_STAGE = "matching"


@dataclass
class MatchingResult:
    matched: list[NormalizedEvidence] = field(default_factory=list)
    rejections: list[EvidenceRejection] = field(default_factory=list)


class StationMatchingEngine:
    # A discovery-provided station hint is accepted directly only when the
    # evidence's own coordinates confirm it within this radius.
    HINT_ACCEPT_RADIUS_M = 500.0

    def __init__(self, settings: Settings, stations: list[Station]) -> None:
        self._settings = settings
        self._stations = stations
        self._by_id = {s.station_id: s for s in stations}
        # Precomputed normalized names per station for fuzzy comparison.
        self._names: dict[str, list[str]] = {
            s.station_id: [normalize_text(n) for n in s.all_names()]
            for s in stations
        }

    def match_all(self, evidence: list[NormalizedEvidence]) -> MatchingResult:
        result = MatchingResult()
        for item in evidence:
            if item.station_candidate.scope == EvidenceScope.BRAND_REGION:
                self._fan_out_region(item, result)
            else:
                self._match_single(item, result)
        return result

    # ---- BRAND_REGION -------------------------------------------------

    def _fan_out_region(
        self, item: NormalizedEvidence, result: MatchingResult
    ) -> None:
        if item.brand == Brand.UNKNOWN:
            result.rejections.append(
                self._reject(item, RejectionReason.STATION_UNMATCHED,
                             "brand-region claim without a brand")
            )
            return
        regions = [r.lower() for r in item.station_candidate.region_hint]
        targets = [
            s for s in self._stations
            if s.brand == item.brand and self._in_region(s, regions)
        ]
        if not targets:
            result.rejections.append(
                self._reject(item, RejectionReason.STATION_UNMATCHED,
                             f"no {item.brand.value} stations in region {regions}")
            )
            return
        for station in targets:
            copy = item.model_copy(deep=True)
            copy.station_id = station.station_id
            result.matched.append(copy)

    @staticmethod
    def _in_region(station: Station, regions: list[str]) -> bool:
        if not regions:
            # An official brand-wide claim with no stated region cannot be
            # scoped; applying it anywhere would be guessing.
            return False
        haystack = f"{station.city} {station.province}".lower()
        return any(region in haystack for region in regions)

    # ---- STATION ------------------------------------------------------

    def _match_single(
        self, item: NormalizedEvidence, result: MatchingResult
    ) -> None:
        # Fast path: a station hint from discovery, validated (not trusted)
        # against brand and the evidence's own coordinates. Anything that
        # fails validation falls through to full scoring.
        hinted = self._validate_hint(item)
        if hinted is not None:
            item.station_id = hinted.station_id
            result.matched.append(item)
            return

        scored = sorted(
            ((self._score(item, s), s) for s in self._stations),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best_station = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if best_score < self._settings.match_accept_score:
            result.rejections.append(
                self._reject(
                    item, RejectionReason.STATION_UNMATCHED,
                    f"best score {best_score:.2f} below "
                    f"{self._settings.match_accept_score} "
                    f"(closest: {best_station.station_id})",
                )
            )
            return
        if best_score - second_score < self._settings.match_ambiguity_margin:
            # Two stations are almost equally plausible. Guessing between
            # them could attach a price to the wrong pump — reject instead.
            result.rejections.append(
                self._reject(
                    item, RejectionReason.STATION_AMBIGUOUS,
                    f"{best_station.station_id} ({best_score:.2f}) vs "
                    f"{scored[1][1].station_id} ({second_score:.2f})",
                )
            )
            return

        item.station_id = best_station.station_id
        result.matched.append(item)

    def _validate_hint(self, item: NormalizedEvidence) -> Station | None:
        hint = item.station_candidate.station_id_hint
        if not hint:
            return None
        station = self._by_id.get(hint)
        if station is None:
            return None
        if item.brand != Brand.UNKNOWN and item.brand != station.brand:
            return None
        candidate = item.station_candidate
        if candidate.latitude is not None and candidate.longitude is not None:
            distance = haversine_m(
                candidate.latitude, candidate.longitude,
                station.latitude, station.longitude,
            )
            if distance > self.HINT_ACCEPT_RADIUS_M:
                return None
            return station
        # No coordinates to confirm with: only accept a hint that the
        # source name doesn't contradict.
        if candidate.name:
            best = max(
                (
                    SequenceMatcher(None, candidate.name, known).ratio()
                    for known in self._names[station.station_id]
                ),
                default=0.0,
            )
            return station if best >= 0.75 else None
        return None

    def _score(self, item: NormalizedEvidence, station: Station) -> float:
        candidate = item.station_candidate

        # Brand is a hard constraint when both sides know it.
        if item.brand != Brand.UNKNOWN and item.brand != station.brand:
            return 0.0

        score = 0.0

        # A station_id hint from discovery is strong but still validated:
        # it only counts when nothing else contradicts it.
        if candidate.station_id_hint == station.station_id:
            score += 0.45

        if candidate.latitude is not None and candidate.longitude is not None:
            distance = haversine_m(
                candidate.latitude, candidate.longitude,
                station.latitude, station.longitude,
            )
            if distance <= 100:
                score += 0.50
            elif distance <= 300:
                score += 0.35
            elif distance <= 750:
                score += 0.15
            elif distance > 2000:
                return 0.0  # coordinates actively contradict this station

        if candidate.name:
            best_name = max(
                (
                    SequenceMatcher(None, candidate.name, known).ratio()
                    for known in self._names[station.station_id]
                ),
                default=0.0,
            )
            token_overlap = max(
                (
                    token_jaccard(candidate.name, known)
                    for known in self._names[station.station_id]
                ),
                default=0.0,
            )
            name_score = max(best_name, token_overlap)
            # An (almost) exact official-name/alias match is strong identity
            # evidence on its own — brand mismatch was already excluded above.
            if name_score >= 0.90:
                score += 0.60
            elif name_score >= 0.75:
                score += 0.30
            elif name_score >= 0.55:
                score += 0.15

        if candidate.address:
            overlap = token_jaccard(candidate.address, station.address)
            if overlap >= 0.60:
                score += 0.20
            elif overlap >= 0.35:
                score += 0.10
            if station.city.lower() in candidate.address:
                score += 0.05

        return min(score, 1.0)

    @staticmethod
    def _reject(
        item: NormalizedEvidence, reason: RejectionReason, detail: str
    ) -> EvidenceRejection:
        log.debug("Match rejection %s: %s", item.evidence_id, detail)
        return EvidenceRejection(
            evidence_id=item.evidence_id,
            stage=_STAGE,
            reason=reason,
            detail=detail,
            provider_name=item.provider_name,
            source_url=item.source_url,
        )
