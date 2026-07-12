"""OCR provider — image intelligence for price board photos.

The OCR backend is an interface; Tesseract is the default implementation
when pytesseract/Pillow are installed. Unreadable or low-confidence
extractions are rejected, never repaired.
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.evidence import RawEvidence
from fie.normalization.timestamps import parse_timestamp
from fie.observability import get_logger
from fie.providers.base import BaseProvider, ProviderFetchError
from fie.providers.http import HttpFetcher, HttpFetchError
from fie.providers.parsing import extract_fuel_prices

log = get_logger("providers.ocr")

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float  # 0..1 mean word confidence
    word_count: int


class OcrBackend(ABC):
    @abstractmethod
    def extract(self, image_bytes: bytes) -> OcrResult: ...

    @abstractmethod
    def is_available(self) -> bool: ...


class TesseractBackend(OcrBackend):
    def is_available(self) -> bool:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False

    def extract(self, image_bytes: bytes) -> OcrResult:
        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT
        )
        words: list[str] = []
        confidences: list[float] = []
        for word, conf in zip(data["text"], data["conf"]):
            word = word.strip()
            conf = float(conf)
            if word and conf >= 0:
                words.append(word)
                confidences.append(conf / 100.0)
        if not words:
            return OcrResult(text="", confidence=0.0, word_count=0)
        return OcrResult(
            text=" ".join(words),
            confidence=sum(confidences) / len(confidences),
            word_count=len(words),
        )


class OCRProvider(BaseProvider):
    name = "ocr"

    def __init__(self, backend: OcrBackend | None = None) -> None:
        self._backend = backend or TesseractBackend()

    def is_available(self) -> bool:
        return self._backend.is_available()

    def can_handle(self, candidate: CandidateSource) -> bool:
        return candidate.requires_ocr or candidate.url.lower().endswith(
            _IMAGE_EXTENSIONS
        )

    async def fetch(
        self, candidate: CandidateSource, ctx: RefreshContext
    ) -> list[RawEvidence]:
        try:
            resource = await HttpFetcher(ctx).fetch(candidate.url)
        except HttpFetchError as exc:
            raise ProviderFetchError(str(exc)) from exc

        result = self._backend.extract(resource.content)
        if result.word_count == 0:
            ctx.trace.record(
                "collection", "ocr_unreadable",
                provider=self.name, url=candidate.url,
            )
            return []
        if result.confidence < ctx.settings.min_ocr_confidence:
            # First gate: don't even emit hopeless extractions. The
            # Normalization Engine enforces the same threshold as a second
            # gate for OCR evidence from any other provider.
            ctx.trace.record(
                "collection", "ocr_low_confidence",
                provider=self.name, url=candidate.url,
                confidence=round(result.confidence, 3),
            )
            return []

        source_timestamp = parse_timestamp(
            str(candidate.metadata.get("source_timestamp", ""))
        )
        evidence = [
            RawEvidence(
                station_candidate=self.station_candidate_from(candidate),
                brand=candidate.brand.value,
                fuel_type_raw=item.fuel_label,
                price_raw=item.price_text,
                source_name=candidate.source_name,
                source_url=candidate.metadata.get("origin_url", candidate.url),
                source_type=candidate.source_type,
                provider_name=self.name,
                source_timestamp=source_timestamp,
                confidence_hint=result.confidence,
                raw_text=item.line,
                metadata={
                    "via_ocr": True,
                    "ocr_confidence": round(result.confidence, 3),
                    "image_url": candidate.url,
                },
            )
            for item in extract_fuel_prices([result.text])
        ]
        log.info(
            "OCR extracted %d price claims from %s (confidence %.2f)",
            len(evidence), candidate.url, result.confidence,
        )
        return evidence
