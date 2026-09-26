"""Calling single upstream APIs (NewsAPI, Cloud Vision) and reporting their failures the way existing Jonah apps expect.

Failure reports keep the exact format of the original Jonah proxy, which the iOS app and Jonah Browser already display:

    HTTP 503   {"error": {"message": "Search request failed: upstream HTTP 429 - <the upstream's own message>", "upstream_status": 429}}

503, not 502: Cloudflare (in front of the service) replaces the body of an origin 502/504 with its own page, hiding the reason.
The message never contains str(exception) (for Google that text includes the request URL, and with it the API key): only the upstream
status and the upstream's own message, with key=... parameters and every configured secret removed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi.responses import JSONResponse

from app.config import Settings
from app.logging_config import log_event
from app.providers.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    retry_after_seconds,
)

log = logging.getLogger("jonah.upstream")

UPSTREAM_ERROR_STATUS = 503
_KEY_PARAM = re.compile(r"(key|apiKey|api_key)=[^&\s\"']+", re.IGNORECASE)
_DAILY = re.compile(r"per day|daily|24.hour", re.IGNORECASE)


def scrub(text: str, settings: Settings) -> str:
    """Remove key=... query parameters and the literal value of every configured secret."""
    text = _KEY_PARAM.sub(r"\1=[redacted]", str(text))
    for secret in settings.secrets_to_redact():
        text = text.replace(secret, "[redacted]")
    return text


def upstream_message(response: httpx.Response) -> str:
    """The upstream's own explanation: body.error.message, body.error (a string), or body.message; else the start of the body."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    reason = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            reason = str(error.get("message") or "")
        elif isinstance(error, str):
            # OAuth style (eBay's token endpoint): {"error": "invalid_client", "error_description": "..."}
            reason = error + (f": {body['error_description']}" if body.get("error_description") else "")
        errors = body.get("errors")  # eBay's APIs: {"errors": [{"message": ..., "longMessage": ...}]}
        if not reason and isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("longMessage") or errors[0].get("message") or "")
        reason = reason or str(body.get("message") or "")
    return reason[:300]


def error_for_status(response: httpx.Response) -> ProviderError:
    status = response.status_code
    message = _KEY_PARAM.sub(r"\1=[redacted]", upstream_message(response)) or f"HTTP {status}"
    if status == 402 or (status == 429 and _DAILY.search(message)):
        return ProviderQuotaExceeded(message, upstream_status=status)
    if status == 429:
        return ProviderRateLimited(message, retry_after=retry_after_seconds(response), upstream_status=status)
    if status in (401, 403):
        return ProviderAuthError(message, upstream_status=status)
    if status >= 500:
        return ProviderUnavailable(message, upstream_status=status)
    return ProviderBadResponse(message, upstream_status=status)


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    """One HTTP call returning parsed JSON. Raises ProviderError; never includes the URL (it may hold a key) in the error."""
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderTimeout(type(exc).__name__) from exc
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(type(exc).__name__) from exc
    if response.status_code >= 400:
        raise error_for_status(response)
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderBadResponse("upstream reply was not JSON", upstream_status=response.status_code) from exc


def upstream_error_response(what: str, error: ProviderError | None, settings: Settings, *, reason: str | None = None, status: int = UPSTREAM_ERROR_STATUS) -> JSONResponse:
    """`what` failed: the original proxy's JSON error, with a safe, readable reason."""
    upstream_status = error.upstream_status if error is not None else None
    text = scrub(reason if reason is not None else (error.message or error.code if error is not None else ""), settings)
    message = f"{what} failed"
    if upstream_status:
        message += f": upstream HTTP {upstream_status}"
    if text:
        message += f" - {text}" if upstream_status else f": {text}"
    log_event(log, "upstream_failed", level=logging.WARNING, what=what, upstream_status=upstream_status, error=(error.code if error else None), reason=text)
    return JSONResponse(status_code=status, content={"error": {"message": message[:400], "upstream_status": upstream_status}})
