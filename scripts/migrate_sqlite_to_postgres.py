"""One-time migration: copy the local SQLite store into Postgres.

Copies verified prices, price history, the adjustment ledger, and provider
reliability so a cloud deployment starts with everything the local engine
has already learned. Existing rows in the target are never overwritten.

Usage:
    FIE_DATABASE_URL=postgresql://... .venv/bin/python scripts/migrate_sqlite_to_postgres.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fie.store.price_store import VerifiedPriceStore  # noqa: E402  (schema init)

SQLITE_PATH = Path(os.environ.get("FIE_DB_PATH", "fie.db"))
DATABASE_URL = os.environ.get("FIE_DATABASE_URL", "")

TABLES = {
    "verified_prices": "(station_id, fuel_type)",
    "price_adjustments": "(brand, fuel_class, effective_date)",
    "provider_reliability": "(provider_name)",
    "price_history": None,  # append-only, no natural key — insert as-is
}


def main() -> None:
    if not DATABASE_URL:
        sys.exit("Set FIE_DATABASE_URL to the target Postgres URL.")
    if not SQLITE_PATH.exists():
        sys.exit(f"No local store at {SQLITE_PATH} — nothing to migrate.")

    # Let the store create the target schema exactly as the app would.
    VerifiedPriceStore(SQLITE_PATH, database_url=DATABASE_URL)

    import psycopg

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    with psycopg.connect(DATABASE_URL) as dst:
        for table, conflict_key in TABLES.items():
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table}: 0 rows (skipped)")
                continue
            columns = [k for k in rows[0].keys() if not (table == "price_history" and k == "id")]
            placeholders = ", ".join(["%s"] * len(columns))
            conflict = f"ON CONFLICT {conflict_key} DO NOTHING" if conflict_key else ""
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders}) {conflict}"
            )
            copied = 0
            with dst.cursor() as cur:
                for row in rows:
                    cur.execute(sql, tuple(row[c] for c in columns))
                    copied += max(cur.rowcount, 0)
            print(f"{table}: {copied}/{len(rows)} rows copied")

    print("Migration complete.")


if __name__ == "__main__":
    main()
