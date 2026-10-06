"""The submission API; the gate worker is a separate process so a gate crash cannot take it down."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import log
from axiom import config_error, init

from . import routes
from .middleware import BodyLimit, RequestContext
from .settings import MAX_REQUEST_BYTES, Settings, load


def create_app(settings: Settings | None = None, store: db.Store | None = None) -> FastAPI:
    # Build the app; tests inject `store`, otherwise the lifespan owns one per process.
    settings = settings or load()

    # Deprecated in typeshed, but FastAPI's `lifespan=` still expects exactly this.
    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Open the store for the app's lifetime, unless a test handed one in.
        owned = store is None
        app.state.store = store or db.connect(
            rate_limit=settings.rate_per_minute,
            rate_window_seconds=settings.rate_window_seconds,
        )
        app.state.settings = settings
        logger.info(f"[api] listening on {settings.host}:{settings.port}")
        try:
            yield
        finally:
            if owned:
                app.state.store.close()

    app = FastAPI(
        title="conjectures-floatlib-competition",
        # No docs or schema endpoints: README documents the API, and they are surface for nothing.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    if store is not None:
        app.state.store = store

    # Outermost first: the body cap refuses before any read; the context wraps everything.
    app.add_middleware(RequestContext)
    app.add_middleware(BodyLimit, max_bytes=MAX_REQUEST_BYTES)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.host_allowlist)
    # No CORS: there is no browser client, so no origin needs to be allowed.

    app.include_router(routes.router)

    @app.exception_handler(HTTPException)
    async def _refusal(_request: Request, exc: HTTPException) -> JSONResponse:
        # Keep the {"detail": {"reason": ...}} shape the miner client reads, dict or string.
        detail = exc.detail if isinstance(exc.detail, dict) else {"reason": str(exc.detail)}
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _malformed(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Name the bad form fields, never echo values (signature, files); loc is the field path.
        errors = cast("list[dict[str, object]]", exc.errors())
        locs = [cast("tuple[object, ...]", e["loc"]) for e in errors]
        fields = sorted({str(loc[-1]) for loc in locs if loc})
        return JSONResponse(
            {"detail": {"reason": f"malformed or missing: {', '.join(fields)}"}}, status_code=422
        )

    return app


# No module-level app, so env is read only via the factory (`uvicorn --factory`) or main().


def main() -> None:
    # Serve the API with uvicorn on the configured host and port.
    import uvicorn

    log.setup()
    events = init("competition-submission-api")
    try:
        settings = load()
    except Exception as exc:
        events.error("service_misconfigured", error=config_error(exc))
        raise
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Honour X-Forwarded-* behind a reverse proxy; harmless when served directly.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("SERVICE_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
