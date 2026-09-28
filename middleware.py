"""Request context: request ID, secure headers, one structured access-log line per request, a safe 500, and a request-body size limit."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.logging_config import log_event, request_id_var

log = logging.getLogger("jonah.http")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class BodySizeLimitMiddleware:
    """Refuse request bodies over `max_bytes` (413) without reading them into memory: checks Content-Length up front and counts the
    bytes of a chunked body as they arrive. Pure ASGI, so it also covers bodies without a Content-Length."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge()  # FastAPI turns this into the 413 response while it reads the body
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = json.dumps({"detail": "request body too large"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def install_middleware(app: FastAPI, settings: Settings) -> None:
    # Added first = innermost: a 413 still passes through request_context below and gets the request id and security headers.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=int(settings.max_request_body_mb * 1024 * 1024))

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - never leak internals; the request id lets the operator find the log line
                log_event(log, "unhandled_error", level=logging.ERROR, error=type(exc).__name__, path=request.url.path)
                response = JSONResponse(status_code=500, content={"detail": "internal server error", "request_id": request_id})
            response.headers["X-Request-ID"] = request_id
            for name, value in getattr(request.state, "rate_limit_headers", {}).items():
                response.headers.setdefault(name, value)  # a 429 already carries its own (Remaining: 0)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            if not request.url.path.startswith(_DOC_PATHS):
                response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
                response.headers["Cache-Control"] = "no-store"
            if settings.is_production:
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            log_event(
                log, "request", method=request.method, path=request.url.path, status=response.status_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )  # fmt: skip  (the query string is deliberately not logged here)
            return response
        finally:
            request_id_var.reset(token)
