"""Shared FastAPI dependencies: access to the service, client identification, authentication and rate limiting."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from app.config import Settings
from app.service import SearchService


def get_service(request: Request) -> SearchService:
    return request.app.state.service


def get_settings_from(request: Request) -> Settings:
    return request.app.state.settings


def client_ip(request: Request, trust_proxy_headers: bool, proxy_hops: int = 1) -> str:
    """The caller's IP. X-Forwarded-For is honoured only when TRUST_PROXY_HEADERS=true (i.e. behind a proxy you control, such as
    Render's load balancer): otherwise a client could put any address in that header and dodge the rate limit.

    Even then the LEFTMOST entry is not trusted: the client can send its own X-Forwarded-For and the proxy only APPENDS the address
    it saw. With N trusted proxies in front of the app (TRUSTED_PROXY_HOPS), the N-th entry from the right is the real client."""
    if trust_proxy_headers:
        chain = [part.strip() for header in request.headers.getlist("x-forwarded-for") for part in header.split(",") if part.strip()]
        if chain:
            return chain[-min(max(1, proxy_hops), len(chain))]
    return request.client.host if request.client else "unknown"


def _bearer_token(request: Request) -> str | None:
    """The key from `Authorization: Bearer <key>`, or from `X-Jonah-Key: <key>` (the header existing Jonah apps send)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    legacy = request.headers.get("x-jonah-key", "").strip()
    return legacy or None


def _key_matches(token: str | None, keys: list[str]) -> bool:
    if not token:
        return False
    supplied = token.encode()
    matched = False
    for key in keys:  # compare against every key, in constant time each, so timing does not reveal which one was close
        matched |= hmac.compare_digest(supplied, key.encode())
    return matched


async def guard(request: Request) -> str:
    """Rate limit, then authenticate. Used by every endpoint except the health/info ones.

    Rate limiting comes first so wrong-key guessing is throttled too. The limit is per API key when a valid key was sent,
    otherwise per client IP. Returns the client id (a hash, never the key itself). The X-RateLimit-* headers are put on the
    response by the middleware (routes return their own JSONResponse, which would drop headers set here)."""
    settings: Settings = request.app.state.settings
    keys = settings.api_keys
    token = _bearer_token(request)
    authenticated = bool(keys) and _key_matches(token, keys)
    if authenticated:
        client_id = "key:" + hashlib.sha256((token or "").encode()).hexdigest()[:16]
    else:
        client_id = "ip:" + client_ip(request, settings.trust_proxy_headers, settings.trusted_proxy_hops)
    decision = await request.app.state.rate_limiter.hit(client_id)
    if decision.limit:
        request.state.rate_limit_headers = {"X-RateLimit-Limit": str(decision.limit), "X-RateLimit-Remaining": str(decision.remaining)}
    if not decision.allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": str(decision.retry_after), "X-RateLimit-Limit": str(decision.limit), "X-RateLimit-Remaining": "0"})
    if keys and not authenticated:
        raise HTTPException(status_code=401, detail="missing or invalid API key (send 'Authorization: Bearer <key>' or 'X-Jonah-Key: <key>')", headers={"WWW-Authenticate": "Bearer"})
    return client_id
