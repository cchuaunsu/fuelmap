"""Normalization Engine.

Turns RawEvidence into NormalizedEvidence, or rejects it with a recorded
reason. Malformed, implausible, or unreadable values never travel further
into the engine.
"""

from __future__ import annotations

import re

from fie.config import Settings
from fie.models.enums import Brand, RejectionReason
from fie.models.evidence import EvidenceRejection, NormalizedEvidence, RawEvidence
from fie.normalization import vocab
from fie.normalization.text import normalize_text
from fie.normalization.timestamps import to_utc
from fie.observability import get_logger

log = get_logger("normalization")

_ACCEPTED_CURRENCIES = {"php", "peso", "pesos", "₱", "p"}
_PRICE_RE = re.compile(r"^\s*(?:₱|php|p)?\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*$", re.IGNORECASE)

_STAGE = "normalization"


class NormalizationEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def normalize(
        self, raw: RawEvidence
    ) -> NormalizedEvidence | EvidenceRejection:
        reject = self._rejector(raw)

        if not raw.fuel_type_raw or not str(raw.price_raw).strip():
            return reject(RejectionReason.MISSING_FIELDS, "empty fuel label or price")

        # Currency: PHP only. Conversion is a future extension point, not a
        # silent assumption.
        currency = raw.currency_raw.strip().lower()
        if currency not in _ACCEPTED_CURRENCIES:
            return reject(
                RejectionReason.UNSUPPORTED_CURRENCY, f"currency={raw.currency_raw!r}"
            )

        fuel_type = vocab.parse_fuel_label(raw.fuel_type_raw)
        if fuel_type is None:
            return reject(
                RejectionReason.UNKNOWN_FUEL_TYPE, f"label={raw.fuel_type_raw!r}"
            )

        price = self._parse_price(str(raw.price_raw))
        if price is None:
            return reject(
                RejectionReason.MALFORMED_PRICE, f"price={raw.price_raw!r}"
            )

        low, high = self._settings.price_bounds[fuel_type]
        if not (low <= price <= high):
            return reject(
                RejectionReason.IMPLAUSIBLE_PRICE,
                f"{price:.2f} outside [{low}, {high}] for {fuel_type.value}",
            )

        via_ocr = bool(raw.metadata.get("via_ocr", False))
        ocr_confidence = raw.metadata.get("ocr_confidence")
        if via_ocr:
            if ocr_confidence is None:
                return reject(
                    RejectionReason.LOW_OCR_CONFIDENCE, "OCR evidence without confidence"
                )
            if float(ocr_confidence) < self._settings.min_ocr_confidence:
                return reject(
                    RejectionReason.LOW_OCR_CONFIDENCE,
                    f"ocr_confidence={float(ocr_confidence):.2f} < "
                    f"{self._settings.min_ocr_confidence}",
                )

        brand = vocab.parse_brand(raw.brand or raw.station_candidate.brand_hint)

        candidate = raw.station_candidate.model_copy()
        if candidate.name:
            candidate.name = normalize_text(candidate.name)
        if candidate.address:
            candidate.address = normalize_text(candidate.address)

        return NormalizedEvidence(
            evidence_id=raw.evidence_id,
            station_candidate=candidate,
            brand=brand if brand != Brand.UNKNOWN else vocab.parse_brand(raw.source_name),
            fuel_type=fuel_type,
            price=round(price, 2),
            source_name=raw.source_name,
            source_url=raw.source_url,
            source_type=raw.source_type,
            provider_name=raw.provider_name,
            retrieval_timestamp=to_utc(raw.retrieval_timestamp),
            source_timestamp=to_utc(raw.source_timestamp) if raw.source_timestamp else None,
            confidence_hint=max(0.0, min(1.0, raw.confidence_hint)),
            via_ocr=via_ocr,
            ocr_confidence=float(ocr_confidence) if ocr_confidence is not None else None,
            raw_text=raw.raw_text,
            metadata=raw.metadata,
        )

    @staticmethod
    def _parse_price(raw: str) -> float | None:
        match = _PRICE_RE.match(raw)
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _rejector(raw: RawEvidence):
        def reject(reason: RejectionReason, detail: str) -> EvidenceRejection:
            log.debug(
                "Rejected evidence %s from %s: %s (%s)",
                raw.evidence_id, raw.provider_name, reason.value, detail,
            )
            return EvidenceRejection(
                evidence_id=raw.evidence_id,
                stage=_STAGE,
                reason=reason,
                detail=detail,
                provider_name=raw.provider_name,
                source_url=raw.source_url,
            )

        return reject
