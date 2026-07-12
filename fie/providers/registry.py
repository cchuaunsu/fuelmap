"""Provider registry: routes each candidate source to one provider.

Providers are checked in registration order, so specific providers
(brand websites, Facebook, OCR) are registered before generic fallbacks.
"""

from __future__ import annotations

from fie.discovery.models import CandidateSource
from fie.observability import get_logger
from fie.providers.base import BaseProvider

log = get_logger("providers.registry")


class ProviderRegistry:
    def __init__(self, providers: list[BaseProvider]) -> None:
        self._providers = providers
        for provider in providers:
            if not provider.is_available():
                log.warning(
                    "Provider %s registered but unavailable "
                    "(missing runtime dependency); it will be skipped",
                    provider.name,
                )

    @property
    def providers(self) -> list[BaseProvider]:
        return list(self._providers)

    def resolve(self, candidate: CandidateSource) -> BaseProvider | None:
        for provider in self._providers:
            if provider.is_available() and provider.can_handle(candidate):
                return provider
        return None
