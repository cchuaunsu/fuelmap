"""Derivation Engine.

When a refresh cannot directly verify a price, the last verified baseline
plus the officially announced adjustments since is still hard knowledge:

    current = verified(₱62.85 on Jul 5) + announced(+₱3.30 effective Jul 8)

This is arithmetic on official statements — not estimation, not
interpolation, not prediction. Derived prices are always stored and served
with the distinct DERIVED status, capped at MEDIUM confidence, and rebuilt
from the immutable baseline on every refresh (never derived from a derived
value). Beyond max_derivation_days without re-verification, derivation
stops and the honest answer returns to "last successfully verified".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fie.config import Settings
from fie.context import RefreshContext
from fie.models.adjustment import FUEL_CLASS_OF, PriceAdjustment
from fie.models.enums import ConfidenceLevel, FuelType
from fie.observability import get_logger
from fie.stationdb.repository import StationRepository
from fie.store.price_store import Derivation, StoredPrice, VerifiedPriceStore

log = get_logger("derivation")

# Confidence decay per adjustment step applied on top of the baseline.
_DECAY_PER_STEP = 0.85


class DerivationEngine:
    def __init__(
        self,
        settings: Settings,
        stations: StationRepository,
        store: VerifiedPriceStore,
    ) -> None:
        self._settings = settings
        self._stations = stations
        self._store = store

    def derive_stale(self, ctx: RefreshContext) -> int:
        """Derive current prices for stale rows where the ledger allows.

        Returns the number of rows upgraded to DERIVED.
        """
        if not self._settings.derivation_enabled:
            return 0

        candidates = self._store.get_stale_rows()
        if not candidates:
            return 0

        adjustments = self._store.get_adjustments()
        if not adjustments:
            return 0

        now = datetime.now(timezone.utc)
        horizon = timedelta(days=self._settings.max_derivation_days)
        derivations: list[Derivation] = []

        for row in candidates:
            outcome = self._derive_row(row, adjustments, now, horizon)
            if outcome is None:
                continue
            price, note, steps, score = outcome
            derivations.append(
                Derivation(
                    station_id=row.station_id,
                    fuel_type=row.fuel_type,
                    price=price,
                    confidence_score=score,
                    confidence_level_value=self._level(score).value,
                    note=note,
                )
            )
            ctx.trace.record(
                "derivation", "derived",
                station_id=row.station_id, fuel_type=row.fuel_type,
                base_price=row.price, derived_price=price,
                adjustment_steps=steps,
            )

        # One transaction for the whole batch — matters when the store is
        # a remote Postgres, where each connection has real latency.
        self._store.apply_derivations(derivations, run_id=ctx.run_id)
        if derivations:
            log.info(
                "Derived %d prices from baselines + adjustment ledger",
                len(derivations),
            )
        return len(derivations)

    def _derive_row(
        self,
        row: StoredPrice,
        adjustments: list[PriceAdjustment],
        now: datetime,
        horizon: timedelta,
    ) -> tuple[float, str, int, float] | None:
        if row.price is None or row.evidence_timestamp is None:
            return None
        baseline_at = datetime.fromisoformat(row.evidence_timestamp)
        if now - baseline_at > horizon:
            return None  # too far from verified ground truth

        station = self._stations.get_by_id(row.station_id)
        if station is None:
            return None
        try:
            fuel = FuelType(row.fuel_type)
        except ValueError:
            return None
        fuel_class = FUEL_CLASS_OF[fuel]
        baseline_date: date = baseline_at.date()

        # Adjustments strictly after the baseline date, applicable to this
        # station's brand (or announced industry-wide). One per effective
        # date: a brand-specific figure beats the generic one.
        by_date: dict[date, PriceAdjustment] = {}
        for adj in adjustments:
            if adj.fuel_class != fuel_class:
                continue
            if adj.effective_date <= baseline_date:
                continue
            if adj.brand not in ("all", station.brand.value):
                continue
            existing = by_date.get(adj.effective_date)
            if existing is None or (existing.brand == "all" and adj.brand != "all"):
                by_date[adj.effective_date] = adj

        if not by_date:
            return None

        steps = sorted(by_date.values(), key=lambda a: a.effective_date)
        price = row.price
        for adj in steps:
            price += adj.delta
        price = round(price, 2)

        low, high = self._settings.price_bounds[fuel]
        if not (low <= price <= high):
            log.warning(
                "Derived price %.2f for %s/%s outside plausibility bounds; refusing",
                price, row.station_id, row.fuel_type,
            )
            return None

        score = round(
            row.confidence_score * (_DECAY_PER_STEP ** len(steps)), 4
        )
        note = (
            f"₱{row.price:.2f} verified {baseline_date.isoformat()} "
            + " ".join(
                f"{'+' if a.delta >= 0 else ''}{a.delta:.2f} eff {a.effective_date.isoformat()}"
                for a in steps
            )
        )
        return price, note, len(steps), score

    def _level(self, score: float) -> ConfidenceLevel:
        # Derived prices never claim HIGH confidence.
        if score >= self._settings.medium_confidence_threshold:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW
