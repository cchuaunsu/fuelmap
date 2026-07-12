"""Evidence Collector.

Runs providers concurrently against discovered candidates with strict fault
isolation: a timeout or crash in one provider is recorded and the rest
continue. The engine always finishes with whatever evidence survived.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fie.context import RefreshContext
from fie.discovery.models import CandidateSource
from fie.models.evidence import RawEvidence
from fie.observability import get_logger
from fie.providers.registry import ProviderRegistry

log = get_logger("collection")


@dataclass
class ProviderError:
    provider: str
    candidate_url: str
    error: str


@dataclass
class CollectionResult:
    evidence: list[RawEvidence] = field(default_factory=list)
    errors: list[ProviderError] = field(default_factory=list)
    candidates_processed: int = 0
    candidates_unhandled: int = 0


class EvidenceCollector:
    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    async def collect(
        self, candidates: list[CandidateSource], ctx: RefreshContext
    ) -> CollectionResult:
        result = CollectionResult()
        semaphore = asyncio.Semaphore(ctx.settings.max_concurrent_fetches)

        await self._run_round(candidates, ctx, result, semaphore)

        # One bounded extra round for candidates derived during collection
        # (e.g. price-board photos found inside social posts).
        derived = ctx.drain_derived_candidates()
        if derived:
            log.info("Processing %d derived candidates (OCR round)", len(derived))
            await self._run_round(derived, ctx, result, semaphore)

        log.info(
            "Collection finished: %d evidence items, %d provider errors, "
            "%d candidates had no capable provider",
            len(result.evidence), len(result.errors), result.candidates_unhandled,
        )
        return result

    async def _run_round(
        self,
        candidates: list[CandidateSource],
        ctx: RefreshContext,
        result: CollectionResult,
        semaphore: asyncio.Semaphore,
    ) -> None:
        tasks = [
            self._collect_one(candidate, ctx, semaphore)
            for candidate in candidates
        ]
        for candidate, outcome in zip(
            candidates, await asyncio.gather(*tasks, return_exceptions=True)
        ):
            result.candidates_processed += 1
            if isinstance(outcome, BaseException):
                # _collect_one already converts expected failures; this
                # catches anything else so the round always completes.
                result.errors.append(
                    ProviderError(
                        provider="unknown",
                        candidate_url=candidate.url,
                        error=repr(outcome),
                    )
                )
            elif outcome is None:
                result.candidates_unhandled += 1
                ctx.trace.record(
                    "collection", "no_provider", url=candidate.url,
                    source_type=candidate.source_type.value,
                )
            elif isinstance(outcome, ProviderError):
                result.errors.append(outcome)
                ctx.trace.record(
                    "collection", "provider_error",
                    provider=outcome.provider, url=outcome.candidate_url,
                    error=outcome.error,
                )
            else:
                result.evidence.extend(outcome)

    async def _collect_one(
        self,
        candidate: CandidateSource,
        ctx: RefreshContext,
        semaphore: asyncio.Semaphore,
    ) -> list[RawEvidence] | ProviderError | None:
        provider = self._registry.resolve(candidate)
        if provider is None:
            return None

        ctx.trace.record(
            "collection", "provider_queried",
            provider=provider.name, url=candidate.url,
        )
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    provider.fetch(candidate, ctx),
                    timeout=ctx.settings.provider_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Provider %s timed out on %s", provider.name, candidate.url
                )
                return ProviderError(
                    provider=provider.name,
                    candidate_url=candidate.url,
                    error=f"timeout after {ctx.settings.provider_timeout_s}s",
                )
            except Exception as exc:
                log.warning(
                    "Provider %s failed on %s: %s",
                    provider.name, candidate.url, exc,
                )
                return ProviderError(
                    provider=provider.name,
                    candidate_url=candidate.url,
                    error=str(exc),
                )
