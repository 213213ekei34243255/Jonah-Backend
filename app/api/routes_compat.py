"""GET /search/web, /search/images, /search/videos: the endpoints of the original Jonah proxy, same behaviour and same JSON.

Existing clients (the Jonah iOS app's CSEResponse model, Jonah Browser's search page) keep working unchanged:

  /search/web     Google Custom Search, Google's JSON returned unchanged.
  /search/images  Google Custom Search with searchType=image, num=10, Google's JSON unchanged.
  /search/videos  What Jonah Browser's Videos tab does: a web search for "<q> site:youtube.com"; if that finds nothing,
                  "<q> video watch".

What is new: when Google fails (above all when its 100-queries-a-day quota is used up), /search/web and /search/videos are answered by
the other configured providers instead, reshaped into Google's format (`items[].title / link / snippet / displayLink`) with an extra
`jonah` object saying which provider answered and why. Image search stays Google-only, like the original. While Google is cooling down
after a quota or key error it is not asked again at all, so a spent quota costs nothing. Failures use the original error format
(see app/upstream.py). Authentication: X-Jonah-Key (the original header) or Authorization: Bearer.
"""

from __future__ import annotations

import html
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.deps import guard
from app.models.search import SearchRequest, SearchResponse
from app.providers.base import ProviderError
from app.providers.google import GoogleProvider
from app.upstream import upstream_error_response

router = APIRouter(tags=["jonah-compatible"])

MAX_QUERY_LENGTH = 500
NOT_CONFIGURED = "Google search is not configured on this server (GOOGLE_API_KEY and GOOGLE_CSE_ID)"
_SITE = re.compile(r"(?<!\S)(-?)site:(\S+)", re.IGNORECASE)

_Q = Annotated[str | None, Query(description="What to search for (required).", examples=["python tutorial"])]


def bad_request(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"message": message, "upstream_status": None}})


def check_query(q: str | None) -> tuple[str, JSONResponse | None]:
    query = " ".join((q or "").split())
    if not query:
        return "", bad_request("Missing required query parameter 'q'")
    if len(query) > MAX_QUERY_LENGTH:
        return "", bad_request(f"Query parameter 'q' is longer than {MAX_QUERY_LENGTH} characters")
    return query, None


async def google_raw(request: Request, params: dict[str, Any]) -> tuple[dict[str, Any] | None, ProviderError | None]:
    """(Google's JSON, None) on success; (None, error) on failure; (None, None) when Google is not configured."""
    manager = request.app.state.manager
    google = manager.provider("google")
    if not isinstance(google, GoogleProvider) or not google.is_configured():
        return None, None
    cooling = manager.cooling_down_error("google")
    if cooling is not None:
        return None, cooling  # quota spent / bad key: answer at once, do not spend another request
    try:
        return await manager.run_direct(google, lambda: google.raw_search(params)), None
    except ProviderError as exc:
        return None, exc


def split_site_operators(query: str) -> tuple[str, list[str], list[str]]:
    """'shoes site:amazon.com -site:ebay.com' -> ('shoes', ['amazon.com'], ['ebay.com']). The fallback applies them as domain filters,
    which every provider honours (Wikipedia would otherwise search for the words 'site:amazon.com')."""
    domains: list[str] = []
    excluded: list[str] = []
    for negative, domain in _SITE.findall(query):
        (excluded if negative else domains).append(domain.strip("()").lower())
    rest = " ".join(_SITE.sub(" ", query).split())
    return rest or query, domains, excluded


def google_shape(query: str, response: SearchResponse, fallback_reason: str) -> dict[str, Any]:
    """Our results in the Custom Search JSON format (the fields Jonah's clients read), plus a `jonah` object explaining the fallback."""
    items = []
    for result in response.results:
        host = re.sub(r"^https?://", "", result.url).split("/")[0].split("?")[0]
        snippet = result.snippet or ""
        items.append(
            {
                "kind": "customsearch#result",
                "title": result.title,
                "htmlTitle": html.escape(result.title),
                "link": result.url,
                "displayLink": host,
                "snippet": snippet,
                "htmlSnippet": html.escape(snippet),
                "formattedUrl": result.url,
                "htmlFormattedUrl": html.escape(result.url),
            }
        )
    seconds = response.metadata.processing_time_ms / 1000
    data: dict[str, Any] = {
        "kind": "customsearch#search",
        "queries": {"request": [{"title": "Jonah Search", "searchTerms": query, "count": len(items), "startIndex": 1}]},
        "searchInformation": {
            "searchTime": seconds, "formattedSearchTime": f"{seconds:.2f}",
            "totalResults": str(len(items)), "formattedTotalResults": str(len(items)),
        },  # fmt: skip
        "jonah": {
            "fallback": True,
            "provider": response.metadata.provider,
            "reason": fallback_reason,
            "errors": [e.model_dump() for e in response.errors],
        },
    }
    if items:  # like Google: no "items" key at all when there are no results
        data["items"] = items
    return data


def describe_google_failure(error: ProviderError | None) -> str:
    if error is None:
        return NOT_CONFIGURED
    status = f" (upstream HTTP {error.upstream_status})" if error.upstream_status else ""
    return f"google: {error.code}{status} - {error.message or error.code}"


async def fallback(request: Request, query: str, what: str, google_error: ProviderError | None) -> JSONResponse:
    """Answer with the other providers, in Google's format. If none can answer either, the original error format."""
    settings = request.app.state.settings
    text, domains, excluded = split_site_operators(query)
    try:
        search = SearchRequest(query=text, max_results=10, domains=domains, exclude_domains=excluded)
    except ValidationError:
        try:  # a site: operator that is not a valid domain: search without the filters rather than refuse
            search = SearchRequest(query=text, max_results=10)
        except ValidationError:
            return bad_request("Invalid query")
    response, status = await request.app.state.service.search(search, getattr(request.state, "request_id", None), exclude=("google",))
    if status == 200:
        return JSONResponse(google_shape(query, response, describe_google_failure(google_error)))
    others = [f"{e.provider}: {e.error}" for e in response.errors if e.provider != "*"]
    if google_error is None:
        reason = NOT_CONFIGURED + ("; other providers failed (" + ", ".join(others) + ")" if others else "; no other provider is configured")
        return upstream_error_response(what, None, settings, reason=reason)
    reason = (google_error.message or google_error.code) + ("; no other provider could answer (" + ", ".join(others) + ")" if others else "")
    return upstream_error_response(what, google_error, settings, reason=reason)


@router.get("/search/web", summary="Web search (Google Custom Search format)")
async def search_web(request: Request, q: _Q = None, _client: str = Depends(guard)):
    query, bad = check_query(q)
    if bad is not None:
        return bad
    data, error = await google_raw(request, {"q": query})
    if data is not None:
        return JSONResponse(data)
    return await fallback(request, query, "Search request", error)


@router.get("/search/images", summary="Image search (Google Custom Search format)")
async def search_images(request: Request, q: _Q = None, _client: str = Depends(guard)):
    query, bad = check_query(q)
    if bad is not None:
        return bad
    data, error = await google_raw(request, {"q": query, "searchType": "image", "num": 10})
    if data is not None:
        return JSONResponse(data)
    settings = request.app.state.settings
    return upstream_error_response("Image search request", error, settings, reason=None if error else NOT_CONFIGURED)


@router.get("/search/videos", summary="Video search (Google Custom Search format)")
async def search_videos(request: Request, q: _Q = None, _client: str = Depends(guard)):
    query, bad = check_query(q)
    if bad is not None:
        return bad
    first, error = await google_raw(request, {"q": f"{query} site:youtube.com"})
    if first is not None and first.get("items"):
        return JSONResponse(first)
    if first is not None:  # Google answered but found nothing on YouTube: the broader second query Jonah's page used to make
        second, _ = await google_raw(request, {"q": f"{query} video watch"})
        return JSONResponse(second if second is not None else first)
    return await fallback(request, f"{query} site:youtube.com", "Video search request", error)
