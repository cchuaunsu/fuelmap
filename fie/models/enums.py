"""Core enumerations shared by every module of the engine."""

from __future__ import annotations

from enum import Enum


class Brand(str, Enum):
    SHELL = "shell"
    PETRON = "petron"
    CALTEX = "caltex"
    SEAOIL = "seaoil"
    UNIOIL = "unioil"
    CLEANFUEL = "cleanfuel"
    PHOENIX = "phoenix"
    FLYING_V = "flying_v"
    TOTAL = "total"
    JETTI = "jetti"
    PTT = "ptt"
    UNKNOWN = "unknown"


class FuelType(str, Enum):
    GASOLINE_91 = "gasoline_ron91"
    GASOLINE_95 = "gasoline_ron95"
    GASOLINE_97 = "gasoline_ron97"
    GASOLINE_100 = "gasoline_ron100"
    DIESEL = "diesel"
    PREMIUM_DIESEL = "premium_diesel"
    KEROSENE = "kerosene"


class SourceType(str, Enum):
    """Where a piece of evidence ultimately comes from.

    Ranking-relevant: OFFICIAL_STATION outranks OFFICIAL_COMPANY, which
    outranks everything else (see the Conflict Resolution Engine).
    """

    OFFICIAL_STATION = "official_station"    # page owned by the specific station
    OFFICIAL_COMPANY = "official_company"    # page owned by the fuel company
    OFFICIAL_SOCIAL = "official_social"      # official social-media account
    AGGREGATOR = "aggregator"                # public price aggregator (e.g. DOE-fed)
    BUSINESS_LISTING = "business_listing"    # public business directory entry
    COMMUNITY = "community"                  # community page / crowd report
    SEARCH_INDEX = "search_index"            # page found through web search
    UNKNOWN = "unknown"


class EvidenceScope(str, Enum):
    """How widely a claim applies.

    STATION: the price claim is about one specific station.
    BRAND_REGION: an official brand-wide claim explicitly scoped to a region
    (e.g. a company publishing its Metro Manila pump prices). This is not
    interpolation — the source itself states the coverage.
    """

    STATION = "station"
    BRAND_REGION = "brand_region"


class RejectionReason(str, Enum):
    MISSING_FIELDS = "missing_fields"
    MALFORMED_PRICE = "malformed_price"
    IMPLAUSIBLE_PRICE = "implausible_price"
    UNKNOWN_FUEL_TYPE = "unknown_fuel_type"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"
    OUTDATED = "outdated"
    UNRELIABLE_PROVIDER = "unreliable_provider"
    DUPLICATE = "duplicate"
    STATION_UNMATCHED = "station_unmatched"
    STATION_AMBIGUOUS = "station_ambiguous"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ResolutionStatus(str, Enum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


class StoreStatus(str, Enum):
    VERIFIED = "verified"
    # A previously verified price kept because every provider failed this
    # refresh. Exposed as "Last Successfully Verified".
    STALE_VERIFIED = "stale_verified"
    # Computed by the Derivation Engine: last verified baseline price plus
    # officially announced adjustments since. Transparent arithmetic on
    # official statements — always labeled, never presented as verified.
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"
