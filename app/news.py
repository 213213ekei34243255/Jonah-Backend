"""NewsAPI (https://newsapi.org) client: top headlines, article search, and the list of news sources (publishers).

Same upstream endpoints and parameters as the original Jonah proxy, and NewsAPI's JSON is returned unchanged, so existing clients (the iOS
app's NewsAPIResponse model) decode it as before. The key is sent in NewsAPI's X-Api-Key header rather than in the URL, so it can never
appear in a logged URL.

Plan limits worth knowing: NewsAPI's free Developer plan allows 100 requests a day and is licensed for development only; a
production app needs a paid plan. Responses are cached for NEWS_CACHE_TTL_SECONDS so repeat requests do not use up the allowance.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from app.config import Settings, secret_value
from app.providers.base import ProviderAuthError, ProviderBadResponse, ProviderQuotaExceeded, ProviderRateLimited
from app.upstream import request_json

NEWSAPI_BASE = "https://newsapi.org/v2"
CATEGORIES = frozenset({"business", "entertainment", "general", "health", "science", "sports", "technology"})
SORT_ORDERS = frozenset({"relevancy", "popularity", "publishedAt"})

_AUTH_CODES = {"apiKeyDisabled", "apiKeyExhausted", "apiKeyInvalid", "apiKeyMissing"}


class NewsApiClient:
    name: ClassVar[str] = "newsapi"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def is_configured(self) -> bool:
        return bool(secret_value(self.settings.news_api_key))

    def timeout_budget(self) -> float:
        return self.settings.request_timeout_seconds

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET {NEWSAPI_BASE}/{path}. Returns NewsAPI's JSON unchanged. Raises ProviderError."""
        headers = {"X-Api-Key": secret_value(self.settings.news_api_key), "Accept": "application/json"}
        data = await request_json(self.client, "GET", f"{NEWSAPI_BASE}/{path}", params=params, headers=headers)
        if not isinstance(data, dict):
            raise ProviderBadResponse("unexpected response shape", upstream_status=200)
        if data.get("status") == "error":  # NewsAPI normally uses HTTP errors, but its body format allows this too
            code, message = str(data.get("code") or ""), str(data.get("message") or "NewsAPI reported an error")
            if code in _AUTH_CODES:
                raise ProviderAuthError(message, upstream_status=200)
            if code == "rateLimited":
                raise ProviderQuotaExceeded(message, upstream_status=200)
            if code == "maximumResultsReached":
                raise ProviderRateLimited(message, upstream_status=200)
            raise ProviderBadResponse(message, upstream_status=200)
        return data
