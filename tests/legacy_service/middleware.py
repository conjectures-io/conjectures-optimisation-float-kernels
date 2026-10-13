"""Everything that wraps a request: a body cap, a request id, and response headers."""

from __future__ import annotations

import uuid

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# Set even on a JSON API, so a browser pointed at a gate report won't sniff or frame it.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class BodyLimit(BaseHTTPMiddleware):
    # Refuse an oversized body from its Content-Length, before anything reads it.

    _max: int

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        # The byte ceiling every request body is held to.
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Refuse an oversized body with a 413 before it is read.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            return JSONResponse(
                {"detail": {"reason": f"the request may be at most {self._max} bytes"}},
                status_code=413,
            )
        return await call_next(request)


class RequestContext(BaseHTTPMiddleware):
    # Give each request an id, log failures with it, and turn any crash into a one-line 500.

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Give each request an id, returned in X-Request-Id and printed in its failure logs.
        request_id = uuid.uuid4().hex[:12]
        route = f"[api] [{request_id}] {request.method} {request.url.path}"
        try:
            response: Response = await call_next(request)
        except Exception:
            logger.exception(f"{route} failed")
            response = JSONResponse(
                {"detail": {"reason": "the validator hit an internal error"}},
                status_code=500,
            )
        response.headers["X-Request-Id"] = request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if response.status_code >= 400:
            logger.info(f"{route} -> {response.status_code}")
        return response
