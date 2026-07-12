"""Official price-adjustment model.

Philippine fuel companies announce per-liter price adjustments weekly
(effective Tuesdays, 6 a.m.). Each announcement is an official statement:
"company X changes fuel class Y by ±Z pesos per liter effective date D".
The engine records these in a persistent ledger and uses them to derive
current prices from verified baselines when direct evidence is stale.
"""

from __future__ import annotations

from datetime import date, datetime
from pydantic import BaseModel

from fie.models.enums import FuelType

# Adjustments are announced per fuel class, not per RON grade.
FUEL_CLASS_OF: dict[FuelType, str] = {
    FuelType.GASOLINE_91: "gasoline",
    FuelType.GASOLINE_95: "gasoline",
    FuelType.GASOLINE_97: "gasoline",
    FuelType.GASOLINE_100: "gasoline",
    FuelType.DIESEL: "diesel",
    FuelType.PREMIUM_DIESEL: "diesel",
    FuelType.KEROSENE: "kerosene",
}


class PriceAdjustment(BaseModel):
    # Brand slug the announcement names, or "all" for industry-wide moves.
    brand: str
    fuel_class: str          # gasoline | diesel | kerosene
    delta: float             # PHP per liter, signed
    effective_date: date
    source_name: str
    source_url: str
    announced_at: datetime | None = None
