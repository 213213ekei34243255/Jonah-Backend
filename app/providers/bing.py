"""Bing Web Search provider (needs BING_API_KEY).

IMPORTANT: Microsoft retired the Bing Search APIs (Bing Web Search API v7) in August 2025 and points customers to its Azure AI Agents
"Grounding with Bing" instead. This provider speaks the v7 REST format and only works if you still have a working v7-compatible
endpoint and key (set BING_ENDPOINT if yours differs). If you do not, leave BING_API_KEY empty and the provider stays disabled.
"""

from __future__ import annotations

from typing import ClassVar

from app.config import secret_value
from app.models.search import SearchResult
from app.providers.base import SearchParams, SearchProvider, result_items
from app.util import hostname_of, parse_datetime

_FRESHNESS = {"hour": "Day", "day": "Day", "week": "Week", "month": "Month"}  # Bing has no 'year' window: ignored


class BingProvider(SearchProvider):
    name: ClassVar[str] = "bing"
    supports_freshness: ClassVar[bool] = True
    supports_language: ClassVar[bool] = True
    max_per_request: ClassVar[int] = 50

    def is_configured(self) -> bool:
        return bool(secret_value(self.settings.bing_api_key)) and bool(self.settings.bing_endpoint.strip())

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        query: dict[str, str | int] = {
            "q": params.query_with_operators(),
            "count": min(params.max_results, self.max_per_request),
            "responseFilter": "Webpages",
            "textDecorations": "false",
        }
        if params.freshness in _FRESHNESS:
            query["freshness"] = _FRESHNESS[params.freshness]
        if params.language:
            query["setLang"] = params.language
        headers = {"Ocp-Apim-Subscription-Key": secret_value(self.settings.bing_api_key), "Accept": "application/json"}
        data = await self._get_json(self.settings.bing_endpoint.strip(), params=query, headers=headers)
        results: list[SearchResult] = []
        for item in result_items(data, "webPages", "value"):
            url = item.get("url")
            if not isinstance(url, str):
                continue
            results.append(
                SearchResult(
                    title=item.get("name") or "",
                    url=url,
                    snippet=item.get("snippet"),
                    source=hostname_of(url) or None,
                    published_at=parse_datetime(item.get("datePublished")) or parse_datetime(item.get("dateLastCrawled")),
                )
            )
        return results
