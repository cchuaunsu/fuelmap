"""Verified Price Store.

Persistence for verified prices, the official price-adjustment ledger,
append-only price history, and provider reliability tracking.

Two interchangeable backends behind one class (the engine never knows
which): SQLite by default (a local file), or Postgres when
FIE_DATABASE_URL is set — used in cloud deployments so history and the
ledger survive host restarts.

Design invariants:
- The price/evidence_timestamp columns hold only directly VERIFIED truth.
  Derived values live in separate derived_* overlay columns, so ground truth
  is never contaminated and derivation is always recomputed from the
  immutable baseline.
- A verified price is never overwritten by evidence older than the evidence
  already backing it.
- When a refresh cannot verify a price that was verified before, the old
  value is kept and marked STALE_VERIFIED, never silently deleted.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from fie.models.adjustment import PriceAdjustment
from fie.models.enums import ResolutionStatus, StoreStatus
from fie.models.verification import ResolvedPrice
from fie.observability import get_logger

log = get_logger("store")

_TABLES = """
CREATE TABLE IF NOT EXISTS verified_prices (
    station_id            TEXT NOT NULL,
    fuel_type             TEXT NOT NULL,
    status                TEXT NOT NULL,
    price                 {REAL},
    currency              TEXT NOT NULL DEFAULT 'PHP',
    confidence_level      TEXT NOT NULL,
    confidence_score      {REAL} NOT NULL DEFAULT 0,
    source_name           TEXT,
    source_url            TEXT,
    source_type           TEXT,
    evidence_timestamp    TEXT,
    verified_at           TEXT,
    last_refresh_at       TEXT NOT NULL,
    last_refresh_run_id   TEXT NOT NULL,
    unavailable_reason    TEXT DEFAULT '',
    derived_price             {REAL},
    derived_confidence_score  {REAL},
    derived_confidence_level  TEXT,
    derivation_note           TEXT DEFAULT '',
    derived_at                TEXT,
    PRIMARY KEY (station_id, fuel_type)
);

CREATE TABLE IF NOT EXISTS price_adjustments (
    brand           TEXT NOT NULL,
    fuel_class      TEXT NOT NULL,
    delta           {REAL} NOT NULL,
    effective_date  TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (brand, fuel_class, effective_date)
);

CREATE TABLE IF NOT EXISTS price_history (
    id                  {AUTOID},
    station_id          TEXT NOT NULL,
    fuel_type           TEXT NOT NULL,
    price               {REAL} NOT NULL,
    confidence_level    TEXT NOT NULL,
    confidence_score    {REAL} NOT NULL,
    source_name         TEXT,
    source_url          TEXT,
    verified_at         TEXT NOT NULL,
    run_id              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_station
    ON price_history (station_id, fuel_type, verified_at);

CREATE TABLE IF NOT EXISTS provider_reliability (
    provider_name   TEXT PRIMARY KEY,
    agreements      INTEGER NOT NULL DEFAULT 0,
    disagreements   INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
"""

_SQLITE_SCHEMA = _TABLES.format(
    REAL="REAL", AUTOID="INTEGER PRIMARY KEY AUTOINCREMENT"
)
_POSTGRES_SCHEMA = _TABLES.format(
    REAL="DOUBLE PRECISION", AUTOID="BIGSERIAL PRIMARY KEY"
)


@dataclass(frozen=True)
class StoredPrice:
    station_id: str
    fuel_type: str
    status: StoreStatus
    price: float | None
    currency: str
    confidence_level: str
    confidence_score: float
    source_name: str | None
    source_url: str | None
    source_type: str | None
    evidence_timestamp: str | None
    verified_at: str | None
    last_refresh_at: str
    unavailable_reason: str
    derived_price: float | None = None
    derived_confidence_score: float | None = None
    derived_confidence_level: str | None = None
    derivation_note: str = ""
    derived_at: str | None = None


@dataclass(frozen=True)
class Derivation:
    """One derived-price overlay, applied in batch after resolution."""

    station_id: str
    fuel_type: str
    price: float
    confidence_score: float
    confidence_level_value: str
    note: str


class VerifiedPriceStore:
    def __init__(self, db_path: Path, database_url: str = "") -> None:
        self._db_path = db_path
        self._database_url = database_url
        self._pg = bool(database_url)
        self._lock = threading.Lock()
        schema = _POSTGRES_SCHEMA if self._pg else _SQLITE_SCHEMA
        with self._lock, self._connect() as conn:
            if self._pg:
                for statement in schema.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                conn.executescript(schema)
        log.info(
            "Verified price store ready (%s)",
            "postgres" if self._pg else f"sqlite at {db_path}",
        )

    def _connect(self):
        if self._pg:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self._database_url, row_factory=dict_row)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _q(self, sql: str) -> str:
        """Translate parameter placeholders for the active backend."""
        return sql.replace("?", "%s") if self._pg else sql

    # ---- writing verified results --------------------------------------

    def apply_resolutions(
        self, results: list[ResolvedPrice], run_id: str
    ) -> dict[str, str]:
        """Apply a whole refresh's resolutions in one transaction.

        Returns {"station_id:fuel_type": action} where action is one of
        'verified', 'kept_newer_existing', 'marked_stale', 'unavailable'.
        """
        now = datetime.now(timezone.utc).isoformat()
        actions: dict[str, str] = {}
        with self._lock, self._connect() as conn:
            for resolved in results:
                action = self._apply_one(conn, resolved, run_id, now)
                actions[f"{resolved.station_id}:{resolved.fuel_type.value}"] = action
        return actions

    def apply_resolution(self, resolved: ResolvedPrice, run_id: str) -> str:
        return self.apply_resolutions([resolved], run_id)[
            f"{resolved.station_id}:{resolved.fuel_type.value}"
        ]

    def _apply_one(self, conn, resolved: ResolvedPrice, run_id: str, now: str) -> str:
        existing = conn.execute(
            self._q(
                "SELECT status, evidence_timestamp FROM verified_prices "
                "WHERE station_id=? AND fuel_type=?"
            ),
            (resolved.station_id, resolved.fuel_type.value),
        ).fetchone()
        has_prior_truth = existing is not None and existing["status"] in (
            StoreStatus.VERIFIED.value,
            StoreStatus.STALE_VERIFIED.value,
        )

        if resolved.status == ResolutionStatus.VERIFIED:
            assert resolved.evidence_timestamp is not None
            if (
                has_prior_truth
                and existing["evidence_timestamp"] is not None
                and existing["evidence_timestamp"]
                > resolved.evidence_timestamp.isoformat()
            ):
                # Stale-data protection: the stored price rests on newer
                # evidence than this refresh produced. Keep it.
                conn.execute(
                    self._q(
                        "UPDATE verified_prices SET last_refresh_at=?, "
                        "last_refresh_run_id=? WHERE station_id=? AND fuel_type=?"
                    ),
                    (now, run_id, resolved.station_id, resolved.fuel_type.value),
                )
                return "kept_newer_existing"

            conn.execute(
                self._q(
                    """
                    INSERT INTO verified_prices (
                        station_id, fuel_type, status, price, currency,
                        confidence_level, confidence_score, source_name,
                        source_url, source_type, evidence_timestamp,
                        verified_at, last_refresh_at, last_refresh_run_id,
                        unavailable_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    ON CONFLICT (station_id, fuel_type) DO UPDATE SET
                        status=excluded.status,
                        price=excluded.price,
                        currency=excluded.currency,
                        confidence_level=excluded.confidence_level,
                        confidence_score=excluded.confidence_score,
                        source_name=excluded.source_name,
                        source_url=excluded.source_url,
                        source_type=excluded.source_type,
                        evidence_timestamp=excluded.evidence_timestamp,
                        verified_at=excluded.verified_at,
                        last_refresh_at=excluded.last_refresh_at,
                        last_refresh_run_id=excluded.last_refresh_run_id,
                        unavailable_reason='',
                        derived_price=NULL,
                        derived_confidence_score=NULL,
                        derived_confidence_level=NULL,
                        derivation_note='',
                        derived_at=NULL
                    """
                ),
                (
                    resolved.station_id,
                    resolved.fuel_type.value,
                    StoreStatus.VERIFIED.value,
                    resolved.price,
                    resolved.currency,
                    resolved.confidence_level.value,
                    resolved.confidence_score,
                    resolved.source_name,
                    resolved.source_url,
                    resolved.source_type,
                    resolved.evidence_timestamp.isoformat(),
                    resolved.verified_at.isoformat() if resolved.verified_at else now,
                    now,
                    run_id,
                ),
            )
            conn.execute(
                self._q(
                    """
                    INSERT INTO price_history (
                        station_id, fuel_type, price, confidence_level,
                        confidence_score, source_name, source_url,
                        verified_at, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    resolved.station_id,
                    resolved.fuel_type.value,
                    resolved.price,
                    resolved.confidence_level.value,
                    resolved.confidence_score,
                    resolved.source_name,
                    resolved.source_url,
                    resolved.verified_at.isoformat() if resolved.verified_at else now,
                    run_id,
                ),
            )
            return "verified"

        # UNAVAILABLE outcome
        if has_prior_truth:
            # Keep the previous verified value as Last Successfully
            # Verified. Derivation overlays are cleared here and rebuilt
            # from the baseline by the Derivation Engine.
            conn.execute(
                self._q(
                    "UPDATE verified_prices SET status=?, last_refresh_at=?, "
                    "last_refresh_run_id=?, unavailable_reason=?, "
                    "derived_price=NULL, derived_confidence_score=NULL, "
                    "derived_confidence_level=NULL, derivation_note='', "
                    "derived_at=NULL "
                    "WHERE station_id=? AND fuel_type=?"
                ),
                (
                    StoreStatus.STALE_VERIFIED.value,
                    now,
                    run_id,
                    resolved.reason,
                    resolved.station_id,
                    resolved.fuel_type.value,
                ),
            )
            return "marked_stale"

        conn.execute(
            self._q(
                """
                INSERT INTO verified_prices (
                    station_id, fuel_type, status, price, currency,
                    confidence_level, confidence_score, source_name,
                    source_url, source_type, evidence_timestamp, verified_at,
                    last_refresh_at, last_refresh_run_id, unavailable_reason
                ) VALUES (?, ?, ?, NULL, 'PHP', ?, 0, NULL, NULL, NULL, NULL,
                          NULL, ?, ?, ?)
                ON CONFLICT (station_id, fuel_type) DO UPDATE SET
                    last_refresh_at=excluded.last_refresh_at,
                    last_refresh_run_id=excluded.last_refresh_run_id,
                    unavailable_reason=excluded.unavailable_reason
                """
            ),
            (
                resolved.station_id,
                resolved.fuel_type.value,
                StoreStatus.UNAVAILABLE.value,
                resolved.confidence_level.value,
                now,
                run_id,
                resolved.reason,
            ),
        )
        return "unavailable"

    # ---- derivation overlay ---------------------------------------------

    def get_stale_rows(self) -> list[StoredPrice]:
        with self._connect() as conn:
            rows = conn.execute(
                self._q(
                    "SELECT * FROM verified_prices "
                    "WHERE status=? AND price IS NOT NULL"
                ),
                (StoreStatus.STALE_VERIFIED.value,),
            ).fetchall()
        return [self._to_stored(row) for row in rows]

    def apply_derivations(self, derivations: list[Derivation], run_id: str) -> None:
        if not derivations:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            for d in derivations:
                conn.execute(
                    self._q(
                        "UPDATE verified_prices SET derived_price=?, "
                        "derived_confidence_score=?, derived_confidence_level=?, "
                        "derivation_note=?, derived_at=?, last_refresh_run_id=? "
                        "WHERE station_id=? AND fuel_type=? AND status=?"
                    ),
                    (
                        d.price,
                        d.confidence_score,
                        d.confidence_level_value,
                        d.note,
                        now,
                        run_id,
                        d.station_id,
                        d.fuel_type,
                        StoreStatus.STALE_VERIFIED.value,
                    ),
                )

    # ---- adjustment ledger ----------------------------------------------

    def record_adjustments(self, adjustments: list[PriceAdjustment]) -> int:
        """Insert new ledger entries; existing (brand, fuel, date) rows win.

        Returns the number of newly recorded entries.
        """
        if not adjustments:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        with self._lock, self._connect() as conn:
            for adj in adjustments:
                cursor = conn.execute(
                    self._q(
                        """
                        INSERT INTO price_adjustments (
                            brand, fuel_class, delta, effective_date,
                            source_name, source_url, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (brand, fuel_class, effective_date)
                        DO NOTHING
                        """
                    ),
                    (
                        adj.brand,
                        adj.fuel_class,
                        adj.delta,
                        adj.effective_date.isoformat(),
                        adj.source_name,
                        adj.source_url,
                        now,
                    ),
                )
                inserted += max(cursor.rowcount, 0)
        return inserted

    def get_adjustments(self, since_days: int = 45) -> list[PriceAdjustment]:
        cutoff = datetime.now(timezone.utc).date().toordinal() - since_days
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM price_adjustments ORDER BY effective_date"
            ).fetchall()
        out: list[PriceAdjustment] = []
        for row in rows:
            effective = date.fromisoformat(row["effective_date"])
            if effective.toordinal() < cutoff:
                continue
            out.append(
                PriceAdjustment(
                    brand=row["brand"],
                    fuel_class=row["fuel_class"],
                    delta=row["delta"],
                    effective_date=effective,
                    source_name=row["source_name"],
                    source_url=row["source_url"],
                )
            )
        return out

    # ---- reading ---------------------------------------------------------

    def get_all(self) -> list[StoredPrice]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verified_prices ORDER BY station_id, fuel_type"
            ).fetchall()
        return [self._to_stored(row) for row in rows]

    def get_station(self, station_id: str) -> list[StoredPrice]:
        with self._connect() as conn:
            rows = conn.execute(
                self._q(
                    "SELECT * FROM verified_prices WHERE station_id=? "
                    "ORDER BY fuel_type"
                ),
                (station_id,),
            ).fetchall()
        return [self._to_stored(row) for row in rows]

    def get_history(
        self, station_id: str, fuel_type: str, limit: int = 100
    ) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                self._q(
                    "SELECT price, confidence_level, source_name, verified_at "
                    "FROM price_history WHERE station_id=? AND fuel_type=? "
                    "ORDER BY verified_at DESC LIMIT ?"
                ),
                (station_id, fuel_type, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _to_stored(row) -> StoredPrice:
        return StoredPrice(
            station_id=row["station_id"],
            fuel_type=row["fuel_type"],
            status=StoreStatus(row["status"]),
            price=row["price"],
            currency=row["currency"],
            confidence_level=row["confidence_level"],
            confidence_score=row["confidence_score"],
            source_name=row["source_name"],
            source_url=row["source_url"],
            source_type=row["source_type"],
            evidence_timestamp=row["evidence_timestamp"],
            verified_at=row["verified_at"],
            last_refresh_at=row["last_refresh_at"],
            unavailable_reason=row["unavailable_reason"] or "",
            derived_price=row["derived_price"],
            derived_confidence_score=row["derived_confidence_score"],
            derived_confidence_level=row["derived_confidence_level"],
            derivation_note=row["derivation_note"] or "",
            derived_at=row["derived_at"],
        )

    # ---- provider reliability ---------------------------------------------

    def get_provider_reliability(self) -> dict[str, float]:
        """Laplace-smoothed agreement rate per provider (0..1)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM provider_reliability").fetchall()
        return {
            row["provider_name"]: (row["agreements"] + 1)
            / (row["agreements"] + row["disagreements"] + 2)
            for row in rows
        }

    def record_provider_outcomes(
        self, agreements: dict[str, int], disagreements: dict[str, int],
        errors: dict[str, int],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        providers = set(agreements) | set(disagreements) | set(errors)
        with self._lock, self._connect() as conn:
            for provider in providers:
                conn.execute(
                    self._q(
                        """
                        INSERT INTO provider_reliability (
                            provider_name, agreements, disagreements,
                            errors, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (provider_name) DO UPDATE SET
                            agreements = agreements + excluded.agreements,
                            disagreements = disagreements + excluded.disagreements,
                            errors = errors + excluded.errors,
                            updated_at = excluded.updated_at
                        """
                    ),
                    (
                        provider,
                        agreements.get(provider, 0),
                        disagreements.get(provider, 0),
                        errors.get(provider, 0),
                        now,
                    ),
                )
