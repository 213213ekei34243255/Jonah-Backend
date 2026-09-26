"""Shared test fixtures. No test touches the network: providers are faked or served by httpx.MockTransport, DNS is faked."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.models.search import SearchResult
from app.providers.base import ProviderError, SearchParams, SearchProvider
from app.scraper.fetcher import FetchedPage, FetchError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A developer's real environment variables (SEARCH_API_KEY, BRAVE_API_KEY, ...) must never leak into a test."""
    for name in [*Settings.model_fields, "google_cse_id"]:
        monkeypatch.delenv(name.upper(), raising=False)


def make_settings(**overrides) -> Settings:
    """Settings with no .env file, no rate limit and Wikipedia off (tests turn on what they need)."""
    values = {"rate_limit_per_minute": 0, "enable_wikipedia": False, "environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def result(title: str, url: str, rank: int = 1, provider: str = "fake", snippet: str | None = "a snippet", published_at: str | None = None) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, source=None, published_at=published_at, rank=rank, provider=provider)


class FakeProvider(SearchProvider):
    """A scripted provider: returns `results`, or raises `error`, optionally after `delay` seconds. Records every call."""

    def __init__(self, name: str = "fake", results: list[SearchResult] | None = None, error: ProviderError | None = None, delay: float = 0.0, configured: bool = True, settings: Settings | None = None):
        super().__init__(settings or make_settings(), client=None)  # type: ignore[arg-type]
        self.name = name  # type: ignore[misc]
        self.results = results or []
        self.error = error
        self.delay = delay
        self.configured = configured
        self.calls: list[SearchParams] = []

    def is_configured(self) -> bool:
        return self.configured

    async def _search(self, params: SearchParams) -> list[SearchResult]:
        self.calls.append(params)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [SearchResult(r.title, r.url, r.snippet, r.source, r.published_at, r.rank, r.provider) for r in self.results]


ARTICLE_HTML = """<html><head><title>The Example Article</title>
<meta property="article:published_time" content="2026-09-20T10:00:00Z"></head><body>
<nav><a href="/">Home</a> <a href="/about">About us</a></nav>
<div id="cookie-banner">We use cookies. Accept all cookies to continue.</div>
<script>var tracking = "SECRET_TRACKING_CODE";</script>
<article><h1>Search engines explained</h1>
<p>{body}</p><p>{body2}</p></article>
<aside class="sidebar">Related links and advertisements</aside>
<footer>Copyright 2026 Example Corp. All rights reserved.</footer></body></html>""".format(
    body="A search engine is a program that finds information on the web by crawling pages, indexing their content and ranking the results for a query. " * 4,
    body2="Modern systems combine many signals, including the words on the page, links between pages and how recently a page was updated. " * 4,
)


class FakeFetcher:
    """Stands in for PageFetcher: returns canned HTML / image bytes (or raises FetchError) and records the URLs it was asked for."""

    def __init__(self, pages: dict[str, str] | None = None, errors: dict[str, str] | None = None, default_html: str = ARTICLE_HTML, images: dict[str, bytes] | None = None):
        self.pages = pages or {}
        self.errors = errors or {}
        self.default_html = default_html
        self.images = images or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if url in self.errors:
            raise FetchError(self.errors[url])
        return FetchedPage(url=url, status=200, content_type="text/html", text=self.pages.get(url, self.default_html))

    async def fetch_image(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        self.calls.append(url)
        if url in self.errors:
            raise FetchError(self.errors[url])
        if url not in self.images:
            raise FetchError("http_error", "404")
        return self.images[url], "image/png"


@pytest.fixture
async def make_client():
    """`client, app = await make_client(settings, providers, fetcher)`: a real app (lifespan started) driven in-process, no sockets.

    `providers=None` means no providers at all; `real_providers=True` builds the real provider classes from the settings instead."""
    opened: list[tuple] = []

    async def _make(settings: Settings | None = None, providers: list[SearchProvider] | None = None, fetcher=None, real_providers: bool = False, **kwargs):
        chosen = None if real_providers else (providers if providers is not None else [])
        app = create_app(settings or make_settings(), providers=chosen, fetcher=fetcher if fetcher is not None else FakeFetcher(), **kwargs)  # kwargs: cache, rate_limiter, upstream_transport
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append((lifespan, client))
        return client, app

    yield _make
    for lifespan, client in reversed(opened):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)
