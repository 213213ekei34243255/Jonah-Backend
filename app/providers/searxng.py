"""SearXNG provider (first-class).

Talks to SearXNG's documented JSON API: GET {instance}/search?q=...&format=json. SEARXNG_URL may list several instances separated by
commas; they are tried in order, and an instance that failed is skipped for a while, so the service does not depend on one public
instance. Note: many public instances disable the JSON format or block automated clients; when an instance answers 403/429 this provider
simply moves to the next one (it never tries to get around a block). The robust setup is your own instance (see README, 'SearXNG').
"""

from __future__ import annotations

import time
from typing import ClassVar

import httpx

from app.models.search import SearchResult
from app.providers.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    SearchParams,
    SearchProvider,
)
from app.util import hostname_of, parse_datetime

_INSTANCE_COOLDOWN = 60.0  # seconds an instance that just failed is skipped
_TIME_RANGE = {"hour": "day", "day": "day", "week": "week", "month": "month", "year": "year"}  # SearXNG has no 'hour'


class SearxngProvider(SearchProvider):
    name: ClassVar[str] = "searxng"
    supports_freshness: ClassVar[bool] = True
    supports_language: ClassVar[bool] = True
    max_per_request: ClassVar[int] = 50

    def __init__(self, settings, client, clock=time.monotonic) -> None:
        super().__init__(settings, client)
        self._clock = clock
        self._down_until: dict[str, float] = {}

    def is_configured(self) -> bool:
        return bool(self.settings.searxng_urls)

    def timeout_budget(self) -> float:
        # sequential instances: allow up to three attempts
        return self.settings.request_timeout_seconds * min(3, max(1, len(self.settings.searxng_urls)))

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        instances = self.settings.searxng_urls
        now = self._clock()
        ordered = [u for u in instances if self._down_until.get(u, 0) <= now] or instances  # if all are 'down', try them anyway
        last_error: ProviderError | None = None
        for base in ordered:
            try:
                results = await self._query_instance(base, params)
            except ProviderError as exc:
                last_error = exc
                self._down_until[base] = self._clock() + _INSTANCE_COOLDOWN
                continue
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(type(exc).__name__)
                self._down_until[base] = self._clock() + _INSTANCE_COOLDOWN
                continue
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(type(exc).__name__)
                self._down_until[base] = self._clock() + _INSTANCE_COOLDOWN
                continue
            self._down_until.pop(base, None)
            return results
        raise last_error or ProviderUnavailable("no SearXNG instance configured")

    async def _query_instance(self, base: str, params: SearchParams) -> list[SearchResult]:
        query: dict[str, str | int] = {"q": params.query_with_operators(), "format": "json", "pageno": 1, "safesearch": 0}
        if params.language:
            query["language"] = params.language
        if params.freshness in _TIME_RANGE:
            query["time_range"] = _TIME_RANGE[params.freshness]
        response = await self.client.get(f"{base}/search", params=query, headers={"Accept": "application/json"})
        if response.status_code in (403, 401):
            # the usual sign that the instance has the JSON format disabled or blocks API clients: not something to work around
            raise ProviderAuthError("instance refused the JSON API (HTTP %d)" % response.status_code)
        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderBadResponse("instance did not return JSON (is the json format enabled?)") from exc
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ProviderBadResponse("JSON has no 'results' list")
        results: list[SearchResult] = []
        for item in data["results"]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            title = item.get("title")
            if not isinstance(url, str) or not isinstance(title, str):
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=item.get("content") if isinstance(item.get("content"), str) else None,
                    source=hostname_of(url) or None,
                    published_at=parse_datetime(item.get("publishedDate")),
                )
            )
        return results
