"""Google Programmable Search (Custom Search JSON API) provider (needs GOOGLE_API_KEY and GOOGLE_CX / GOOGLE_CSE_ID).

The free tier allows 100 queries per day; when Google answers 429 / 'Quota exceeded' this provider raises quota_exceeded and the manager
stops asking it for an hour. Keep it LAST in PROVIDER_PRIORITY so the quota is spent only when the other providers fail.
Google's error text can echo the request URL (with the key): only Google's own message, with any key= scrubbed, is ever surfaced.

`raw_search()` returns Google's JSON exactly as Google sent it. The Jonah-compatible endpoints (/search/web, /search/images,
/search/videos) use it so existing clients keep receiving the same shape; `_search()` normalises it for /search.
Google results are never cached (`cacheable = False`): the Google APIs terms do not allow keeping copies beyond the cache header.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx

from app.config import secret_value
from app.models.search import SearchResult
from app.providers.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SearchParams,
    SearchProvider,
    result_items,
    retry_after_seconds,
)
from app.util import hostname_of, parse_datetime

ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_FRESHNESS = {"hour": "d1", "day": "d1", "week": "w1", "month": "m1", "year": "y1"}  # dateRestrict; Google's finest window is a day
_QUOTA_WORDS = re.compile(r"quota|rate ?limit|daily limit|queries per", re.IGNORECASE)


def _google_message(response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = str((error.get("message") if isinstance(error, dict) else error) or "")
    except ValueError:
        message = ""
    return re.sub(r"(key|apikey)=[^&\s\"']+", r"\1=[redacted]", message, flags=re.IGNORECASE)[:300]


class GoogleProvider(SearchProvider):
    name: ClassVar[str] = "google"
    supports_freshness: ClassVar[bool] = True
    supports_language: ClassVar[bool] = True
    max_per_request: ClassVar[int] = 10  # one request = up to 10 results (and one unit of quota)
    cacheable: ClassVar[bool] = False

    def is_configured(self) -> bool:
        return bool(secret_value(self.settings.google_api_key)) and bool(self.settings.google_cx.strip())

    async def raw_search(self, params: dict[str, Any]) -> dict[str, Any]:
        """One Custom Search call with `params` (q, searchType, num, ...). Returns Google's JSON unchanged. Raises ProviderError."""
        query = {"key": secret_value(self.settings.google_api_key), "cx": self.settings.google_cx.strip(), **params}
        try:
            response = await self.client.get(ENDPOINT, params=query)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc  # never str(exc): it contains the URL, and the URL the key
        status = response.status_code
        if status >= 400:
            message = _google_message(response)
            if status == 429 or (status == 403 and _QUOTA_WORDS.search(message)):
                # A DAILY quota stays exhausted until it resets (midnight Pacific): long cooldown. Anything else (per-minute limits) is short.
                if re.search(r"per day|daily", message, re.IGNORECASE):
                    raise ProviderQuotaExceeded(message or "daily quota exceeded", upstream_status=status)
                raise ProviderRateLimited(message or "rate limited", retry_after=retry_after_seconds(response), upstream_status=status)
            if status in (400, 401, 403):
                raise ProviderAuthError(message or f"HTTP {status}", upstream_status=status)
            if status >= 500:
                raise ProviderUnavailable(message or f"HTTP {status}", upstream_status=status)
            raise ProviderBadResponse(message or f"HTTP {status}", upstream_status=status)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderBadResponse("upstream reply was not JSON", upstream_status=status) from exc
        if not isinstance(data, dict):
            raise ProviderBadResponse("unexpected response shape", upstream_status=status)
        return data

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        query: dict[str, str | int] = {"q": params.query_with_operators(), "num": min(params.max_results, self.max_per_request)}
        if params.freshness in _FRESHNESS:
            query["dateRestrict"] = _FRESHNESS[params.freshness]
        if params.language:
            query["lr"] = "lang_" + params.language.split("-")[0].lower()
        data = await self.raw_search(query)
        results: list[SearchResult] = []
        for item in result_items(data, "items"):
            url = item.get("link")
            if not isinstance(url, str):
                continue
            pagemap = item.get("pagemap")
            tags = pagemap.get("metatags") if isinstance(pagemap, dict) else None
            meta = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], dict) else {}
            published = meta.get("article:published_time") or meta.get("og:updated_time") or meta.get("date")
            results.append(
                SearchResult(
                    title=item.get("title") or "",
                    url=url,
                    snippet=item.get("snippet"),
                    source=item.get("displayLink") or hostname_of(url) or None,
                    published_at=parse_datetime(published),
                )
            )
        return results
