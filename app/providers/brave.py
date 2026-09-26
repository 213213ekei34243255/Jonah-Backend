"""Brave Search API provider (needs BRAVE_API_KEY). https://api.search.brave.com/app/documentation/web-search"""

from __future__ import annotations

from typing import ClassVar

from app.config import secret_value
from app.models.search import SearchResult
from app.providers.base import SearchParams, SearchProvider, result_items
from app.util import hostname_of, parse_datetime

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_FRESHNESS = {"hour": "pd", "day": "pd", "week": "pw", "month": "pm", "year": "py"}  # Brave's finest window is 24 hours


class BraveProvider(SearchProvider):
    name: ClassVar[str] = "brave"
    supports_freshness: ClassVar[bool] = True
    supports_language: ClassVar[bool] = True
    max_per_request: ClassVar[int] = 20

    def is_configured(self) -> bool:
        return bool(secret_value(self.settings.brave_api_key))

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        query: dict[str, str | int] = {"q": params.query_with_operators(), "count": min(params.max_results, self.max_per_request), "text_decorations": "false"}
        if params.freshness in _FRESHNESS:
            query["freshness"] = _FRESHNESS[params.freshness]
        if params.language:
            query["search_lang"] = params.language.split("-")[0].lower()
        headers = {"Accept": "application/json", "X-Subscription-Token": secret_value(self.settings.brave_api_key)}
        data = await self._get_json(ENDPOINT, params=query, headers=headers)
        results: list[SearchResult] = []
        for item in result_items(data, "web", "results"):
            url = item.get("url")
            if not isinstance(url, str):
                continue
            profile = item.get("profile") or {}
            meta = item.get("meta_url") or {}
            results.append(
                SearchResult(
                    title=item.get("title") or "",
                    url=url,
                    snippet=item.get("description"),
                    source=profile.get("name") or meta.get("hostname") or hostname_of(url) or None,
                    published_at=parse_datetime(item.get("page_age")) or parse_datetime(item.get("age")),
                )
            )
        return results
