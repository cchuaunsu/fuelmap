"""Official adjustment-record view.

Philippine pump prices move only through officially announced weekly
adjustments. Given the last verified price of a (station, fuel) and the
adjustment ledger, the record predicts what today's price should be:

    expected = last_verified + sum(announced deltas since)

The LedgerView is built once per refresh (from pre-refresh store state) and
consulted by the Confidence Engine:
- a cluster matching the expectation is corroborated by the record;
- a cluster deviating far from it is challenged (downgraded);
- evidence that predates no adjustment is still current, whatever the clock
  says.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fie.config import Settings
from fie.models.adjustment import FUEL_CLASS_OF, PriceAdjustment
from fie.models.enums import FuelType
from fie.models.station import Station
from fie.normalization.timestamps import PHT

# Priors older than this cannot anchor a record expectation.
_MAX_PRIOR_AGE_DAYS = 30


@dataclass(frozen=True)
class RecordAssessment:
    expected_price: float | None
    deviation: float
    consistent: bool          # matches the record's expectation
    deviates: bool            # far enough off to challenge
    ledger_has_data: bool
    applicable_effective_dates: tuple[date, ...]

    def adjusted_since(self, ts: datetime) -> bool:
        """Did any applicable adjustment take effect after this moment?

        Effective dates are Philippine calendar dates, so the comparison
        happens in PHT.
        """
        cutoff = ts.astimezone(PHT).date()
        return any(d > cutoff for d in self.applicable_effective_dates)


class LedgerView:
    def __init__(
        self,
        settings: Settings,
        stations: list[Station],
        priors: dict[tuple[str, str], tuple[float, datetime]],
        adjustments: list[PriceAdjustment],
    ) -> None:
        self._settings = settings
        self._brand_of = {s.station_id: s.brand.value for s in stations}
        self._priors = priors
        self._ledger_has_data = bool(adjustments)
        # (brand|'all', fuel_class) -> [(effective_date, delta)]
        self._by_key: dict[tuple[str, str], list[tuple[date, float]]] = {}
        for adj in adjustments:
            self._by_key.setdefault((adj.brand, adj.fuel_class), []).append(
                (adj.effective_date, adj.delta)
            )

    def assess(
        self, station_id: str, fuel_type: FuelType, price: float
    ) -> RecordAssessment | None:
        brand = self._brand_of.get(station_id)
        if brand is None:
            return None
        fuel_class = FUEL_CLASS_OF[fuel_type]
        applicable = self._applicable(brand, fuel_class)
        effective_dates = tuple(d for d, _ in applicable)

        prior = self._priors.get((station_id, fuel_type.value))
        expected: float | None = None
        deviation = 0.0
        if prior is not None:
            prior_price, prior_ts = prior
            age = datetime.now(timezone.utc) - prior_ts
            if age <= timedelta(days=_MAX_PRIOR_AGE_DAYS):
                prior_date = prior_ts.astimezone(PHT).date()
                expected = round(
                    prior_price
                    + sum(delta for d, delta in applicable if d > prior_date),
                    2,
                )
                deviation = round(abs(price - expected), 2)

        return RecordAssessment(
            expected_price=expected,
            deviation=deviation,
            consistent=(
                expected is not None
                and deviation <= self._settings.record_consistency_tolerance
            ),
            deviates=(
                expected is not None
                and deviation > self._settings.record_challenge_threshold
            ),
            ledger_has_data=self._ledger_has_data,
            applicable_effective_dates=effective_dates,
        )

    def _applicable(
        self, brand: str, fuel_class: str
    ) -> list[tuple[date, float]]:
        """Per effective date, a brand-specific figure beats the generic."""
        by_date: dict[date, tuple[bool, float]] = {}
        for key_brand in ("all", brand):
            for effective, delta in self._by_key.get((key_brand, fuel_class), []):
                specific = key_brand != "all"
                existing = by_date.get(effective)
                if existing is None or (specific and not existing[0]):
                    by_date[effective] = (specific, delta)
        return sorted((d, delta) for d, (_, delta) in by_date.items())
