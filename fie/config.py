"""Engine configuration.

Everything tunable lives here and can be overridden with FIE_* environment
variables, so behavior changes never require code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from fie.models.enums import FuelType

_PACKAGE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FIE_{name}", default)


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes")


# Plausibility bounds in PHP per liter. Evidence outside these bounds is
# rejected as corrupted/implausible, never "corrected".
DEFAULT_PRICE_BOUNDS: dict[FuelType, tuple[float, float]] = {
    FuelType.GASOLINE_91: (35.0, 120.0),
    FuelType.GASOLINE_95: (35.0, 125.0),
    FuelType.GASOLINE_97: (35.0, 130.0),
    FuelType.GASOLINE_100: (40.0, 140.0),
    FuelType.DIESEL: (30.0, 110.0),
    FuelType.PREMIUM_DIESEL: (30.0, 120.0),
    FuelType.KEROSENE: (30.0, 130.0),
}


@dataclass(frozen=True)
class Settings:
    # Data
    stations_path: Path = _PACKAGE_DIR / "data" / "stations.json"
    known_sources_path: Path = _PACKAGE_DIR / "data" / "known_sources.json"
    db_path: Path = Path(_env("DB_PATH", "fie.db"))
    # Static frontend directory served at "/" when it exists.
    web_dir: Path = Path(_env("WEB_DIR", str(_PACKAGE_DIR.parent / "web")))

    # HTTP
    user_agent: str = _env(
        "USER_AGENT",
        "FuelIntelligenceEngine/0.1 (+public fuel price research)",
    )
    http_timeout_s: float = _env_float("HTTP_TIMEOUT_S", 15.0)
    http_retries: int = _env_int("HTTP_RETRIES", 2)
    max_concurrent_fetches: int = _env_int("MAX_CONCURRENT_FETCHES", 8)
    provider_timeout_s: float = _env_float("PROVIDER_TIMEOUT_S", 30.0)

    # Evidence validity. Providers of weekly-cadence sources (e.g. the DOE
    # advisory cycle) may declare a longer allowance per evidence item via
    # metadata["max_age_hours"]; the ceiling bounds every such override.
    max_evidence_age_hours: float = _env_float("MAX_EVIDENCE_AGE_HOURS", 72.0)
    max_evidence_age_ceiling_hours: float = _env_float(
        "MAX_EVIDENCE_AGE_CEILING_HOURS", 240.0
    )
    price_bounds: dict[FuelType, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_PRICE_BOUNDS)
    )
    # Prices within this many pesos are treated as the same claim.
    price_agreement_tolerance: float = _env_float("PRICE_TOLERANCE", 0.011)

    # OCR
    min_ocr_confidence: float = _env_float("MIN_OCR_CONFIDENCE", 0.60)

    # Confidence thresholds
    high_confidence_threshold: float = _env_float("HIGH_CONFIDENCE", 0.70)
    medium_confidence_threshold: float = _env_float("MEDIUM_CONFIDENCE", 0.45)
    # Below this score no price is verifiable — "Price unavailable" wins.
    min_verifiable_score: float = _env_float("MIN_VERIFIABLE_SCORE", 0.35)
    # Providers whose historical reliability drops below this are excluded.
    min_provider_reliability: float = _env_float("MIN_PROVIDER_RELIABILITY", 0.20)

    # Matching
    match_accept_score: float = _env_float("MATCH_ACCEPT_SCORE", 0.55)
    match_ambiguity_margin: float = _env_float("MATCH_AMBIGUITY_MARGIN", 0.15)

    # Adjustment-record checks: a price within the consistency tolerance of
    # (last verified + announced deltas) is corroborated by the record; one
    # deviating beyond the challenge threshold is downgraded.
    record_consistency_tolerance: float = _env_float("RECORD_CONSISTENCY_TOLERANCE", 0.25)
    record_challenge_threshold: float = _env_float("RECORD_CHALLENGE_THRESHOLD", 2.00)
    # Population challenge: a price this far (fraction) from its brand's
    # same-fuel median across the region is flagged and cannot score HIGH.
    population_outlier_fraction: float = _env_float("POPULATION_OUTLIER_FRACTION", 0.10)
    population_outlier_min_sample: int = _env_int("POPULATION_OUTLIER_MIN_SAMPLE", 5)

    # Derivation: when direct evidence is stale, advance the last verified
    # baseline with officially announced adjustments from the ledger.
    derivation_enabled: bool = _env_bool("DERIVATION_ENABLED", True)
    max_derivation_days: int = _env_int("MAX_DERIVATION_DAYS", 14)
    # RSS index used to discover weekly price-adjustment announcements.
    adjustment_feed_url: str = _env(
        "ADJUSTMENT_FEED_URL",
        "https://data.gmanetwork.com/gno/rss/money/feed.xml",
    )

    # Minimum seconds between refresh investigations. With the password
    # gate on, this is only a double-tap guard for the sources.
    refresh_cooldown_s: float = _env_float("REFRESH_COOLDOWN_S", 15.0)

    # When set, the entire site (pages + API) requires this password.
    access_password: str = _env("ACCESS_PASSWORD", "")

    # Optional Postgres URL (e.g. a free Neon database). When set, the
    # verified-price store — including price history and the adjustment
    # ledger — lives there and survives host restarts; otherwise SQLite.
    database_url: str = _env("DATABASE_URL", "")

    # Developer mode (must be enabled here before the API honors it)
    developer_mode_enabled: bool = _env_bool("DEVELOPER_MODE", False)

    # Optional external services (absent = the strategy quietly no-ops)
    serper_api_key: str = _env("SERPER_API_KEY", "")
    searx_url: str = _env("SEARX_URL", "")
    facebook_graph_token: str = _env("FACEBOOK_GRAPH_TOKEN", "")

    log_level: str = _env("LOG_LEVEL", "INFO")


def load_settings() -> Settings:
    return Settings()
