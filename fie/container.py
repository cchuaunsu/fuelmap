"""Composition root: wires every module together.

This is the only place that knows about concrete implementations. Swapping
a component (different station repository, another OCR backend, a new
provider) happens here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from fie.collection.collector import EvidenceCollector
from fie.config import Settings, load_settings
from fie.confidence.engine import ConfidenceEngine
from fie.derivation.engine import DerivationEngine
from fie.discovery.engine import DiscoveryEngine
from fie.discovery.strategies import KnownSourceStrategy, WebSearchStrategy
from fie.normalization.engine import NormalizationEngine
from fie.observability import configure_logging
from fie.pipeline.orchestrator import RefreshOrchestrator
from fie.providers.adjustments import GmaRssAdjustmentProvider
from fie.providers.base import BaseProvider
from fie.providers.brand_websites import ALL_BRAND_WEBSITE_PROVIDERS
from fie.providers.facebook import FacebookPageProvider
from fie.providers.gaswatch import GasWatchProvider
from fie.providers.generic import (
    BusinessListingProvider,
    GenericWebPageProvider,
    GoogleDiscoveryProvider,
)
from fie.providers.ocr import OCRProvider
from fie.providers.registry import ProviderRegistry
from fie.resolution.engine import ConflictResolutionEngine
from fie.stationdb.repository import JsonStationRepository, StationRepository
from fie.store.price_store import VerifiedPriceStore
from fie.verification.engine import VerificationEngine


@dataclass
class EngineContainer:
    settings: Settings
    stations: StationRepository
    store: VerifiedPriceStore
    orchestrator: RefreshOrchestrator


def build_container(settings: Settings | None = None) -> EngineContainer:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    stations = JsonStationRepository(settings.stations_path)
    store = VerifiedPriceStore(settings.db_path, database_url=settings.database_url)

    discovery = DiscoveryEngine(
        strategies=[
            KnownSourceStrategy(settings.known_sources_path),
            WebSearchStrategy(
                serper_api_key=settings.serper_api_key,
                searx_url=settings.searx_url,
            ),
        ]
    )

    # Registration order matters: specific providers before generic
    # fallbacks. OCR first so image candidates never fall through to a
    # text provider.
    providers: list[BaseProvider] = [OCRProvider(), GasWatchProvider()]
    providers += [cls() for cls in ALL_BRAND_WEBSITE_PROVIDERS]
    providers += [
        FacebookPageProvider(graph_token=settings.facebook_graph_token),
        BusinessListingProvider(),
        GoogleDiscoveryProvider(),
        GenericWebPageProvider(),
    ]

    orchestrator = RefreshOrchestrator(
        settings=settings,
        stations=stations,
        discovery=discovery,
        collector=EvidenceCollector(ProviderRegistry(providers)),
        normalizer=NormalizationEngine(settings),
        verifier=VerificationEngine(settings),
        confidence=ConfidenceEngine(settings),
        resolver=ConflictResolutionEngine(settings),
        store=store,
        derivation=DerivationEngine(settings, stations, store),
        adjustment_providers=[
            GmaRssAdjustmentProvider(settings.adjustment_feed_url),
        ],
    )
    return EngineContainer(
        settings=settings,
        stations=stations,
        store=store,
        orchestrator=orchestrator,
    )
