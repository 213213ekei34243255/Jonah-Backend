"""The provider abstraction.

A provider turns one search request into a list of normalised SearchResult objects, or raises a ProviderError with a stable `code`.
Everything that is the same for every provider (HTTP error mapping, result clean-up, ranking positions) lives here, so a new provider is
a small class that only knows its own API. See README, "Adding a provider".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from app.config import Settings
from app.models.search import SearchResult
from app.util import strip_tags


class ProviderError(Exception):
    """A provider call failed. `code` is stable: timeout | unavailable | quota_exceeded | rate_limited | auth_failed | malformed_response."""

    code = "unavailable"

    def __init__(self, message: str = "", *, retry_after: float | None = None, upstream_status: int | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message
        self.retry_after = retry_after
        self.upstream_status = upstream_status  # the upstream HTTP status, when there was a response


class ProviderTimeout(ProviderError):
    code = "timeout"


class ProviderUnavailable(ProviderError):
    code = "unavailable"


class ProviderQuotaExceeded(ProviderError):
    code = "quota_exceeded"


class ProviderRateLimited(ProviderError):
    code = "rate_limited"


class ProviderAuthError(ProviderError):
    code = "auth_failed"


class ProviderBadResponse(ProviderError):
    code = "malformed_response"


ERROR_CLASSES: tuple[type[ProviderError], ...] = (
    ProviderTimeout, ProviderUnavailable, ProviderQuotaExceeded, ProviderRateLimited, ProviderAuthError, ProviderBadResponse,
)  # fmt: skip


@dataclass(slots=True)
class SearchParams:
    """What a provider is asked for. `query` is exactly what the user typed; providers that support site operators call
    `query_with_operators()` to fold the domain filters into the query text."""

    query: str
    max_results: int
    freshness: str = "any"
    language: str | None = None
    domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()

    def query_with_operators(self) -> str:
        text = self.query
        if self.domains:
            sites = " OR ".join(f"site:{d}" for d in self.domains)
            text += f" ({sites})" if len(self.domains) > 1 else f" {sites}"
        for domain in self.exclude_domains:
            text += f" -site:{domain}"
        return text


class SearchProvider(ABC):
    name: ClassVar[str] = "base"
    supports_freshness: ClassVar[bool] = False
    supports_language: ClassVar[bool] = False
    supports_site_operators: ClassVar[bool] = True
    max_per_request: ClassVar[int] = 20
    # False when the provider's terms do not allow keeping its results (Google: "no cached copies longer than the cache header").
    cacheable: ClassVar[bool] = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    # ---- what a subclass provides

    @abstractmethod
    def is_configured(self) -> bool:
        """True when every setting this provider needs is present."""

    @abstractmethod
    async def _search(self, params: SearchParams) -> list[SearchResult]:
        """Call the upstream API and return results in the provider's own order (rank/provider are filled in afterwards)."""

    def timeout_budget(self) -> float:
        """Seconds the manager allows for one `search` call."""
        return self.settings.request_timeout_seconds

    # ---- the public entry point

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        freshness: str = "any",
        language: str | None = None,
        domains: tuple[str, ...] = (),
        exclude_domains: tuple[str, ...] = (),
    ) -> list[SearchResult]:
        params = SearchParams(query, max_results, freshness, language, tuple(domains), tuple(exclude_domains))
        try:
            raw = await self._search(params)
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            # only the exception CLASS: its text can contain the request URL (and with it an API key)
            raise ProviderUnavailable(type(exc).__name__) from exc
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderBadResponse(f"unexpected response shape ({type(exc).__name__})") from exc
        return self._finalize(raw, max_results)

    # ---- helpers for subclasses

    def _finalize(self, raw: list[SearchResult], max_results: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in raw:
            url = (item.url or "").strip()
            title = strip_tags(item.title)
            if not url.lower().startswith(("http://", "https://")) or not title:
                continue
            results.append(
                SearchResult(
                    title=title[:300],
                    url=url,
                    snippet=(strip_tags(item.snippet)[:600] or None) if item.snippet else None,
                    source=item.source,
                    published_at=item.published_at,
                    rank=len(results) + 1,
                    provider=self.name,
                )
            )
            if len(results) >= max_results:
                break
        return results

    async def _get_json(self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        response = await self.client.get(url, params=params, headers=headers)
        self._raise_for_status(response)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderBadResponse("response was not valid JSON") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        retry_after = retry_after_seconds(response)
        if status in (401, 403):
            raise ProviderAuthError(f"HTTP {status}", upstream_status=status)
        if status == 402:
            raise ProviderQuotaExceeded("HTTP 402", upstream_status=status)
        if status == 429:
            raise ProviderRateLimited("HTTP 429", retry_after=retry_after, upstream_status=status)
        if status >= 500:
            raise ProviderUnavailable(f"HTTP {status}", retry_after=retry_after, upstream_status=status)
        raise ProviderBadResponse(f"HTTP {status}", upstream_status=status)

    def describe(self) -> dict[str, Any]:
        """Public facts about the provider. Never includes a key."""
        return {
            "name": self.name,
            "configured": self.is_configured(),
            "supports_freshness": self.supports_freshness,
            "supports_language": self.supports_language,
        }


def result_items(data: Any, *path: str) -> list[dict[str, Any]]:
    """The result objects at data[path[0]][path[1]]...

    A MISSING key means "no results" (APIs omit the list when nothing matched). A value of the WRONG TYPE means the API changed or
    returned an error page, and is reported as malformed_response instead of looking like an empty search. Entries that are not
    objects are skipped.
    """
    node = data
    for key in path:
        if not isinstance(node, dict):
            raise ProviderBadResponse(f"unexpected response shape: expected an object holding '{key}'")
        node = node.get(key)
        if node is None:
            return []
    if not isinstance(node, list):
        raise ProviderBadResponse(f"unexpected response shape: '{'.'.join(path)}' is not a list")
    return [item for item in node if isinstance(item, dict)]


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The Retry-After header as seconds (None when absent or not a number)."""
    value = response.headers.get("retry-after", "")
    return float(value) if value.replace(".", "", 1).isdigit() else None
