"""REST API routes — a thin consumer of the engine, no business logic."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from fie import __version__
from fie.api.schemas import (
    PRICE_UNAVAILABLE,
    RefreshRequest,
    RefreshResponse,
    StationOut,
    StationPricesOut,
    VerifiedPriceOut,
)
from fie.container import EngineContainer
from fie.models.enums import ResolutionStatus, StoreStatus
from fie.models.station import Station
from fie.models.verification import ResolvedPrice
from fie.observability import get_logger
from fie.store.price_store import StoredPrice

log = get_logger("api")

router = APIRouter(prefix="/api/v1")

_STATUS_DISPLAY = {
    StoreStatus.VERIFIED: "verified",
    StoreStatus.STALE_VERIFIED: "last_successfully_verified",
    StoreStatus.DERIVED: "derived",
    StoreStatus.UNAVAILABLE: "price_unavailable",
}


def _container(request: Request) -> EngineContainer:
    return request.app.state.container


def _station_out(station: Station) -> StationOut:
    return StationOut(
        station_id=station.station_id,
        brand=station.brand.value,
        official_name=station.official_name,
        known_aliases=station.known_aliases,
        latitude=station.latitude,
        longitude=station.longitude,
        address=station.address,
        city=station.city,
        province=station.province,
    )


def _stored_price_out(stored: StoredPrice, station: Station) -> VerifiedPriceOut:
    # A stale row with a derivation overlay is served as DERIVED: the last
    # verified baseline advanced by officially announced adjustments.
    status = stored.status
    price = stored.price
    confidence = stored.confidence_level
    confidence_score = stored.confidence_score
    derivation_note = ""
    if stored.status == StoreStatus.STALE_VERIFIED and stored.derived_price is not None:
        status = StoreStatus.DERIVED
        price = stored.derived_price
        confidence = stored.derived_confidence_level or "low"
        confidence_score = stored.derived_confidence_score or 0.0
        derivation_note = stored.derivation_note

    available = status in (
        StoreStatus.VERIFIED, StoreStatus.STALE_VERIFIED, StoreStatus.DERIVED
    )
    return VerifiedPriceOut(
        station_id=station.station_id,
        brand=station.brand.value,
        station_name=station.official_name,
        latitude=station.latitude,
        longitude=station.longitude,
        fuel_type=stored.fuel_type,
        verified_price=price if available else None,
        display=(
            f"₱{price:.2f}/L" if available and price is not None
            else PRICE_UNAVAILABLE
        ),
        status=_STATUS_DISPLAY[status],
        confidence=confidence,
        confidence_score=confidence_score,
        verification_timestamp=stored.verified_at,
        last_refresh_timestamp=stored.last_refresh_at,
        source_used=stored.source_name,
        source_url=stored.source_url,
        unavailable_reason=stored.unavailable_reason,
        derivation_note=derivation_note,
    )


def _resolved_out(resolved: ResolvedPrice, station: Station) -> VerifiedPriceOut:
    verified = resolved.status == ResolutionStatus.VERIFIED
    return VerifiedPriceOut(
        station_id=station.station_id,
        brand=station.brand.value,
        station_name=station.official_name,
        latitude=station.latitude,
        longitude=station.longitude,
        fuel_type=resolved.fuel_type.value,
        verified_price=resolved.price if verified else None,
        display=(
            f"₱{resolved.price:.2f}/L" if verified and resolved.price is not None
            else PRICE_UNAVAILABLE
        ),
        status="verified" if verified else "price_unavailable",
        confidence=resolved.confidence_level.value,
        confidence_score=resolved.confidence_score,
        verification_timestamp=(
            resolved.verified_at.isoformat() if resolved.verified_at else None
        ),
        last_refresh_timestamp=(
            resolved.verified_at.isoformat() if resolved.verified_at else ""
        ),
        source_used=resolved.source_name,
        source_url=resolved.source_url,
        unavailable_reason=resolved.reason,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
async def status(request: Request) -> dict:
    """Deploy/diagnostics probe: which build is live and is the store
    populated. Open (no password) like /health; exposes only counts."""
    summary = await asyncio.to_thread(_container(request).store.summary)
    return {
        "status": "ok",
        "version": __version__,
        "refresh_running": _refresh_lock.locked(),
        "last_refresh_duration_s": _last_refresh_duration_s,
        **summary,
    }


@router.get("/stations", response_model=list[StationOut])
def list_stations(request: Request) -> list[StationOut]:
    container = _container(request)
    return [_station_out(s) for s in container.stations.get_all()]


@router.get("/stations/{station_id}", response_model=StationOut)
def get_station(station_id: str, request: Request) -> StationOut:
    station = _container(request).stations.get_by_id(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Unknown station")
    return _station_out(station)


@router.get("/stations/{station_id}/prices", response_model=StationPricesOut)
def get_station_prices(station_id: str, request: Request) -> StationPricesOut:
    container = _container(request)
    station = container.stations.get_by_id(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Unknown station")
    stored = container.store.get_station(station_id)
    return StationPricesOut(
        station=_station_out(station),
        prices=[_stored_price_out(p, station) for p in stored],
    )


@router.get("/prices", response_model=list[VerifiedPriceOut])
def list_prices(request: Request) -> list[VerifiedPriceOut]:
    container = _container(request)
    stations = {s.station_id: s for s in container.stations.get_all()}
    return [
        _stored_price_out(stored, stations[stored.station_id])
        for stored in container.store.get_all()
        if stored.station_id in stations
    ]


# Public-exposure guardrails: refreshes hit external sources, so they are
# serialized (one at a time) and rate-limited regardless of who asks.
_refresh_lock = asyncio.Lock()
_last_refresh_started: float = 0.0
_last_refresh_duration_s: float | None = None


def _record_refresh_duration(started_monotonic: float) -> None:
    global _last_refresh_duration_s
    _last_refresh_duration_s = round(time.monotonic() - started_monotonic, 1)


async def bootstrap_store_if_needed(container: EngineContainer) -> None:
    """Self-heal the store with a refresh, off the startup path.

    A first deployment (or a wiped database) otherwise serves an empty map
    until a person clicks Refresh and keeps the tab open — and a host that
    sleeps between visits wakes up serving old prices. Uses the same lock
    and cooldown stamp as the endpoint so the two can never overlap.
    """
    global _last_refresh_started
    try:
        summary = await asyncio.to_thread(container.store.summary)
        reason = _bootstrap_reason(summary, container.settings)
        if reason is None or _refresh_lock.locked():
            return
        # A first investigation into an empty store has no stored baseline
        # or provider track record to corroborate against, so its
        # confidence starts low by design. A second pass right after
        # scores against the just-stored baseline and reaches steady-state
        # confidence immediately.
        passes = 1 if summary["prices_stored"] else 2
        async with _refresh_lock:
            for attempt in range(1, passes + 1):
                if attempt > 1:
                    await asyncio.sleep(container.settings.refresh_cooldown_s)
                _last_refresh_started = time.monotonic()
                log.info(
                    "%s — running bootstrap refresh (pass %d/%d)",
                    reason, attempt, passes,
                )
                report = await container.orchestrator.refresh()
                _record_refresh_duration(_last_refresh_started)
                log.info(
                    "Bootstrap refresh %s done: %d verified",
                    report.run_id, report.stats.get("verified", 0),
                )
    except Exception:
        log.exception("Bootstrap refresh failed; serving whatever the "
                      "store already holds")


def _bootstrap_reason(summary: dict, settings) -> str | None:
    if not summary["prices_stored"]:
        return "Store is empty"
    max_age_h = settings.bootstrap_max_age_h
    if not max_age_h or not summary["last_refresh"]:
        return None
    age_h = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(summary["last_refresh"])
    ).total_seconds() / 3600.0
    if age_h > max_age_h:
        return f"Store data is {age_h:.1f}h old (limit {max_age_h:g}h)"
    return None


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(body: RefreshRequest, request: Request) -> RefreshResponse:
    global _last_refresh_started
    container = _container(request)

    cooldown = container.settings.refresh_cooldown_s
    if _refresh_lock.locked():
        raise HTTPException(
            status_code=429,
            detail="A refresh is already running — try again shortly.",
        )
    remaining = cooldown - (time.monotonic() - _last_refresh_started)
    if _last_refresh_started and remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Refreshed recently — prices only change via weekly "
                f"adjustments. Try again in {int(remaining) + 1}s."
            ),
        )

    stations = {s.station_id: s for s in container.stations.get_all()}
    if body.station_ids:
        unknown = [sid for sid in body.station_ids if sid not in stations]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"Unknown station ids: {unknown}"
            )

    async with _refresh_lock:
        _last_refresh_started = time.monotonic()
        report = await container.orchestrator.refresh(
            station_ids=body.station_ids,
            developer_mode=body.developer_mode,
        )
        _record_refresh_duration(_last_refresh_started)
    return RefreshResponse(
        run_id=report.run_id,
        started_at=report.started_at,
        finished_at=report.finished_at,
        stations_processed=report.stations_processed,
        stats=report.stats,
        results=[
            _resolved_out(r, stations[r.station_id])
            for r in report.results
            if r.station_id in stations
        ],
        provider_errors=report.provider_errors,
        developer=(
            {"trace": report.trace, "store_actions": report.store_actions}
            if report.trace is not None
            else None
        ),
    )
