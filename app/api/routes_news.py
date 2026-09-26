"""News through NewsAPI, with NewsAPI's JSON returned unchanged.

  GET /news/headlines?country=in[&category=sports]
      The original Jonah proxy's endpoint, same logic: top headlines for the country (20 articles); if NewsAPI has none for it, the
      newest articles about "technology" (or about the category, when one is given).
  GET /news/search?q=...[&sources=bbc-news,reuters][&domains=...][&excludeDomains=...][&language=en][&sortBy=publishedAt]
                        [&from=2026-09-01][&to=...][&page=1][&pageSize=20]
      Article search (NewsAPI /everything).
  GET /news/sources[?country=in][&category=technology][&language=en]
      The source finder: the news publishers NewsAPI covers, with their ids for /news/search?sources=.

Failures use the original proxy's format: HTTP 503 {"error": {"message": "News request failed: upstream HTTP 401 - ...", ...}}.
Answers are cached for NEWS_CACHE_TTL_SECONDS (NewsAPI's free plan allows only 100 requests a day).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import guard
from app.api.routes_compat import bad_request
from app.news import CATEGORIES, SORT_ORDERS
from app.providers.base import ProviderError
from app.upstream import upstream_error_response

router = APIRouter(tags=["news"])

WHAT = "News request"
NOT_CONFIGURED = "NewsAPI is not configured on this server (NEWS_API_KEY)"
_TWO_LETTERS = re.compile(r"^[a-z]{2}$")
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,60}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class InvalidParameter(ValueError):
    pass


def _two_letters(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip().lower()
    if not _TWO_LETTERS.match(value):
        raise InvalidParameter(f"Invalid '{name}': use a two-letter code such as 'in', 'us' or 'gb'")
    return value


def _category(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip().lower()
    if value not in CATEGORIES:
        raise InvalidParameter(f"Invalid 'category': use one of {', '.join(sorted(CATEGORIES))}")
    return value


def _list(value: str | None, name: str, pattern: re.Pattern[str], limit: int = 20) -> str | None:
    if value is None or not value.strip():
        return None
    items = [part.strip().lower() for part in value.split(",") if part.strip()]
    if len(items) > limit or not all(pattern.match(item) for item in items):
        raise InvalidParameter(f"Invalid '{name}': a comma-separated list of at most {limit} items")
    return ",".join(items)


def _date(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        (datetime if "T" in value else date).fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidParameter(f"Invalid '{name}': use an ISO 8601 date such as 2026-09-01 or 2026-09-01T12:00:00") from exc
    return value


async def _newsapi(request: Request, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """One NewsAPI call through the health tracker, served from the cache when possible. Raises ProviderError."""
    state = request.app.state
    key = "news:" + path + ":" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    cached = await state.cache.get(key)
    if cached is not None:
        return cached
    cooling = state.manager.cooling_down_error(state.news.name)
    if cooling is not None:
        raise cooling
    data = await state.manager.run_direct(state.news, lambda: state.news.get(path, params))
    await state.cache.set(key, data, state.settings.news_cache_ttl_seconds)
    return data


def _not_configured(request: Request) -> JSONResponse | None:
    if request.app.state.news.is_configured():
        return None
    return upstream_error_response(WHAT, None, request.app.state.settings, reason=NOT_CONFIGURED)


@router.get("/news/headlines", summary="Top headlines (NewsAPI format)")
async def headlines(
    request: Request,
    country: Annotated[str | None, Query(description="Two-letter country code (default: NEWS_DEFAULT_COUNTRY, 'in').")] = None,
    category: Annotated[str | None, Query(description="business, entertainment, general, health, science, sports or technology.")] = None,
    _client: str = Depends(guard),
):
    settings = request.app.state.settings
    try:
        chosen_country = _two_letters(country, "country") or _two_letters(settings.news_default_country, "country") or "in"
        chosen_category = _category(category)
    except InvalidParameter as exc:
        return bad_request(str(exc))
    if (missing := _not_configured(request)) is not None:
        return missing
    params: dict[str, Any] = {"country": chosen_country, "pageSize": 20}
    if chosen_category:
        params["category"] = chosen_category
    try:
        primary = await _newsapi(request, "top-headlines", params)
        if primary.get("articles"):
            return JSONResponse(primary)
        # The original fallback: when NewsAPI has no headlines for this country, the newest articles on a broad topic.
        topic = chosen_category if chosen_category and chosen_category != "general" else "technology"
        return JSONResponse(await _newsapi(request, "everything", {"q": topic, "pageSize": 20, "sortBy": "publishedAt"}))
    except ProviderError as exc:
        return upstream_error_response(WHAT, exc, settings)


@router.get("/news/search", summary="Search news articles (NewsAPI format)")
async def news_search(
    request: Request,
    q: Annotated[str | None, Query(description="Keywords or phrase. Required unless sources or domains is given.")] = None,
    sources: Annotated[str | None, Query(description="Comma-separated source ids from /news/sources (max 20).")] = None,
    domains: Annotated[str | None, Query(description="Comma-separated domains, e.g. bbc.co.uk,techcrunch.com.")] = None,
    exclude_domains: Annotated[str | None, Query(alias="excludeDomains", description="Comma-separated domains to leave out.")] = None,
    language: Annotated[str | None, Query(description="Two-letter language code, e.g. en.")] = None,
    sort_by: Annotated[str | None, Query(alias="sortBy", description="relevancy, popularity or publishedAt.")] = None,
    date_from: Annotated[str | None, Query(alias="from", description="Oldest article date (ISO 8601).")] = None,
    date_to: Annotated[str | None, Query(alias="to", description="Newest article date (ISO 8601).")] = None,
    page: Annotated[int, Query(ge=1, le=100)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    _client: str = Depends(guard),
):
    settings = request.app.state.settings
    query = " ".join((q or "").split())
    try:
        params: dict[str, Any] = {
            "sources": _list(sources, "sources", _SOURCE_ID),
            "domains": _list(domains, "domains", _DOMAIN),
            "excludeDomains": _list(exclude_domains, "excludeDomains", _DOMAIN),
            "language": _two_letters(language, "language"),
            "from": _date(date_from, "from"),
            "to": _date(date_to, "to"),
        }
        if sort_by and sort_by not in SORT_ORDERS:
            raise InvalidParameter(f"Invalid 'sortBy': use one of {', '.join(sorted(SORT_ORDERS))}")
    except InvalidParameter as exc:
        return bad_request(str(exc))
    if len(query) > 500:
        return bad_request("Query parameter 'q' is longer than 500 characters")
    if not query and not params["sources"] and not params["domains"]:
        return bad_request("Missing required query parameter 'q' (or 'sources' / 'domains')")
    if (missing := _not_configured(request)) is not None:
        return missing
    params = {k: v for k, v in params.items() if v}
    params.update({"page": page, "pageSize": page_size})
    if query:
        params["q"] = query
    if sort_by:
        params["sortBy"] = sort_by
    try:
        return JSONResponse(await _newsapi(request, "everything", params))
    except ProviderError as exc:
        return upstream_error_response(WHAT, exc, settings)


@router.get("/news/sources", summary="News sources (publishers) NewsAPI covers")
async def news_sources(
    request: Request,
    country: Annotated[str | None, Query(description="Two-letter country code.")] = None,
    category: Annotated[str | None, Query(description="business, entertainment, general, health, science, sports or technology.")] = None,
    language: Annotated[str | None, Query(description="Two-letter language code.")] = None,
    _client: str = Depends(guard),
):
    settings = request.app.state.settings
    try:
        params = {"country": _two_letters(country, "country"), "category": _category(category), "language": _two_letters(language, "language")}
    except InvalidParameter as exc:
        return bad_request(str(exc))
    if (missing := _not_configured(request)) is not None:
        return missing
    try:
        return JSONResponse(await _newsapi(request, "top-headlines/sources", {k: v for k, v in params.items() if v}))
    except ProviderError as exc:
        return upstream_error_response(WHAT, exc, settings)
