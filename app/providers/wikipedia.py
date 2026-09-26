"""Wikipedia search provider: keyless, officially documented (MediaWiki Action API), and a legitimate last resort.

It exists so the service returns something useful with ZERO configuration and as a final fallback when every other provider is down or
out of quota. It only knows encyclopedic content, so keep it last in PROVIDER_PRIORITY. Disable with ENABLE_WIKIPEDIA=false.
Wikimedia's API etiquette requires a descriptive User-Agent (USER_AGENT) and modest request rates.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote

from app.models.search import SearchResult
from app.providers.base import SearchParams, SearchProvider, result_items
from app.util import strip_tags


class WikipediaProvider(SearchProvider):
    name: ClassVar[str] = "wikipedia"
    supports_language: ClassVar[bool] = True
    supports_site_operators: ClassVar[bool] = False  # 'site:' means nothing here; the service filters by domain afterwards
    max_per_request: ClassVar[int] = 20

    def is_configured(self) -> bool:
        return self.settings.enable_wikipedia

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        language = (params.language or "en").split("-")[0].lower()
        if not language.isalpha() or not 2 <= len(language) <= 3:
            language = "en"
        base = f"https://{language}.wikipedia.org"
        query = {
            "action": "query", "list": "search", "srsearch": params.query, "srlimit": min(params.max_results, self.max_per_request),
            "srprop": "snippet", "format": "json", "formatversion": 2, "utf8": 1,
        }  # fmt: skip
        data = await self._get_json(f"{base}/w/api.php", params=query, headers={"Accept": "application/json"})
        results: list[SearchResult] = []
        for item in result_items(data, "query", "search"):
            title = item.get("title")
            if not isinstance(title, str) or not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=f"{base}/wiki/{quote(title.replace(' ', '_'), safe='_(),:')}",
                    snippet=strip_tags(item.get("snippet")),
                    source=f"{language}.wikipedia.org",
                    published_at=None,
                )
            )
        return results
