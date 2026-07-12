"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from fie import __version__
from fie.api.routes import bootstrap_store_if_needed, router
from fie.container import EngineContainer, build_container


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Populate an empty/stale store in the background; startup (and
    # /health) must not wait on it.
    task = asyncio.create_task(bootstrap_store_if_needed(app.state.container))
    yield
    task.cancel()


def create_app(container: EngineContainer | None = None) -> FastAPI:
    app = FastAPI(
        title="Fuel Intelligence Engine",
        version=__version__,
        lifespan=_lifespan,
        description=(
            "Evidence-based verification engine for fuel pump prices. "
            "Every price returned has been discovered, collected, "
            "normalized, matched, verified, and conflict-resolved — or is "
            "honestly reported as unavailable."
        ),
    )
    app.state.container = container or build_container()
    # ~5k price rows compress ~10x; well worth it for map loads.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    settings = app.state.container.settings
    if settings.access_password:
        # Outermost layer: everything (pages + API) behind the password.
        from fie.api.auth import PasswordGate

        app.add_middleware(PasswordGate, password=settings.access_password)
    app.include_router(router)

    # The map frontend is just another consumer of the engine's API.
    # API routes are registered first, so they take precedence.
    web_dir = app.state.container.settings.web_dir
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
