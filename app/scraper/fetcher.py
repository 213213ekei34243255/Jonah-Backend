"""Responsible page fetching.

SafeHttpClient does one GET with every protection this service promises:
  * only http/https, allowed ports, no credentials, no internal host names          (security.validate_url)
  * every address the host resolves to must be public, re-checked on every redirect (security.resolve_public_ips)
  * the connection goes to the VALIDATED IP address, with the original name in the Host header and for TLS (SNI + certificate check),
    so DNS cannot change its answer between the check and the connection (no DNS rebinding)
  * redirects are followed by hand, at most MAX_REDIRECTS, each hop validated (and robots-checked) again
  * a hard cap on the (decompressed) bytes read, a per-page time limit, and a whitelist of content types

PageFetcher adds robots.txt (respected) and a clear User-Agent. Nothing here tries to defeat a website's protections: if a page refuses
automated access (robots, 403, CAPTCHA page, paywall) the result is simply an error code for that page.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from app.config import Settings
from app.scraper.robots import MAX_ROBOTS_BYTES, RobotsChecker
from app.scraper.security import Resolver, UnsafeURLError, resolve_public_ips, validate_url

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_MAX_ADDRESSES_TRIED = 3

HopCheck = Callable[[str], Awaitable[None]]


class FetchError(Exception):
    """A page could not be fetched. `code` is stable and machine-readable (it becomes `content_error` in the API)."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class SafeResponse:
    status: int
    url: str  # the final URL (after redirects)
    content_type: str
    charset: str | None
    body: bytes

    @property
    def text(self) -> str:
        try:
            return self.body.decode(self.charset or "utf-8", errors="replace")
        except LookupError:  # unknown charset name
            return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    text: str


def _format_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    return f"[{ip}]" if ip.version == 6 else str(ip)


class SafeHttpClient:
    def __init__(self, settings: Settings, *, resolver: Resolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,  # redirects are followed by hand so each hop is validated
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(max_connections=max(20, settings.max_concurrent_fetches * 2), max_keepalive_connections=0),
            headers={"User-Agent": settings.user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
        max_bytes: int | None = None,
        hop_check: HopCheck | None = None,
        allowed_types: tuple[str, ...] | None = ALLOWED_CONTENT_TYPES,
    ) -> SafeResponse:
        """GET `url` safely. Raises FetchError. A 4xx/5xx status is returned (not raised): the caller decides what it means."""
        limit = max_bytes or self.settings.max_page_size_bytes
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                return await self._follow(url, accept, limit, hop_check, allowed_types)
        except TimeoutError as exc:
            raise FetchError("timeout") from exc

    async def _follow(self, url: str, accept: str, limit: int, hop_check: HopCheck | None, allowed_types: tuple[str, ...] | None) -> SafeResponse:
        current = url
        for _ in range(self.settings.max_redirects + 1):
            try:
                parts = validate_url(current, self.settings.allowed_port_set)
                addresses = await resolve_public_ips(parts.host, parts.port, self._resolver)  # unsafe targets stop HERE, before robots.txt
                if hop_check is not None:
                    await hop_check(current)
            except UnsafeURLError as exc:
                raise FetchError(exc.code, str(exc)) from exc
            outcome = await self._request_once(parts, addresses, accept, limit, allowed_types)
            if isinstance(outcome, str):  # a redirect: the Location header
                current = urljoin(current, outcome)
                continue
            outcome.url = current
            return outcome
        raise FetchError("too_many_redirects")

    async def _request_once(self, parts, addresses, accept: str, limit: int, allowed_types: tuple[str, ...] | None) -> SafeResponse | str:
        headers = {"Host": parts.host_header, "Accept": accept, "Accept-Encoding": "gzip, deflate"}
        extensions = {"sni_hostname": parts.host} if parts.scheme == "https" else {}
        last_error: Exception | None = None
        for ip in addresses[:_MAX_ADDRESSES_TRIED]:
            target = f"{parts.scheme}://{_format_ip(ip)}:{parts.port}{parts.path_query}"
            try:
                async with self._client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("http_error", f"{response.status_code} without a Location header")
                        return location
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if allowed_types is not None and response.status_code < 400 and content_type and not content_type.startswith(allowed_types):
                        raise FetchError("unsupported_content_type", content_type)
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise FetchError("page_too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():  # decompressed bytes: a "zip bomb" is stopped by the same limit
                        body += chunk
                        if len(body) > limit:
                            raise FetchError("page_too_large")
                    return SafeResponse(response.status_code, "", content_type, response.charset_encoding, bytes(body))
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc  # try the next validated address
                continue
            except httpx.TimeoutException as exc:
                raise FetchError("timeout") from exc
            except httpx.TooManyRedirects as exc:  # never raised (redirects are manual) but harmless to map
                raise FetchError("too_many_redirects") from exc
            except httpx.HTTPError as exc:
                raise FetchError("connection_error", type(exc).__name__) from exc
        raise FetchError("connection_error", type(last_error).__name__ if last_error else "no address")


class PageFetcher:
    """SafeHttpClient + robots.txt. `fetch(url)` returns the page text (HTML) or raises FetchError."""

    def __init__(self, settings: Settings, *, resolver: Resolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.http = SafeHttpClient(settings, resolver=resolver, transport=transport)
        token = settings.user_agent.split("/")[0].strip() or "JonahSearchBot"
        self.robots = RobotsChecker(self._robots_get, token, fail_open=settings.robots_fail_open)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _robots_get(self, url: str) -> tuple[int, str]:
        response = await self.http.get(url, accept="text/plain", max_bytes=MAX_ROBOTS_BYTES, allowed_types=None)
        return response.status, response.text

    async def _robots_hop(self, target: str) -> None:
        if self.settings.respect_robots:
            decision = await self.robots.allowed(target)
            if not decision.allowed:
                raise FetchError(decision.reason or "robots_disallowed")

    async def fetch(self, url: str) -> FetchedPage:
        response = await self.http.get(url, hop_check=self._robots_hop)
        if response.status >= 400:
            raise FetchError("http_error", str(response.status))
        return FetchedPage(url=response.url, status=response.status, content_type=response.content_type, text=response.text)

    async def fetch_image(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Download an image (same SSRF protection, robots.txt and redirect rules as pages). Returns (bytes, content type)."""
        response = await self.http.get(url, accept="image/*", max_bytes=max_bytes, hop_check=self._robots_hop, allowed_types=("image/",))
        if response.status >= 400:
            raise FetchError("http_error", str(response.status))
        if not response.content_type.startswith("image/"):
            raise FetchError("unsupported_content_type", response.content_type or "none")
        if not response.body:
            raise FetchError("empty_response")
        return response.body, response.content_type
