"""The search service: the pipeline behind POST/GET /search.

    request -> cache -> provider manager -> domain filters -> de-duplicate -> rank -> [optional] fetch + extract pages -> response

It never raises for a provider or page failure: those become `errors[]` (per provider) and `content_error` (per page).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Protocol

from app.cache.base import CacheBackend
from app.config import Settings
from app.logging_config import log_event
from app.metrics import SEARCH_DURATION, SEARCH_REQUESTS
from app.models.search import ErrorItem, Metadata, ResultItem, SearchRequest, SearchResponse
from app.providers.base import SearchParams
from app.providers.manager import ManagerOutcome, ProviderManager
from app.ranking.dedup import dedup_key, merge_results
from app.ranking.ranker import rank_results
from app.scraper.extractor import extract_content
from app.scraper.fetcher import FetchedPage, FetchError
from app.util import domain_matches, hostname_of

log = logging.getLogger("jonah.search")

_CONTENT_FIELDS = {"content", "content_error", "content_truncated"}


class PageSource(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...

    async def fetch_image(self, url: str, max_bytes: int) -> tuple[bytes, str]: ...


class SearchService:
    def __init__(self, settings: Settings, manager: ProviderManager, cache: CacheBackend, fetcher: PageSource) -> None:
        self.settings = settings
        self.manager = manager
        self.cache = cache
        self.fetcher = fetcher
        self._extract_lock = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def search(self, request: SearchRequest, request_id: str | None = None, *, exclude: tuple[str, ...] = ()) -> tuple[SearchResponse, int]:
        """Returns (response, http_status). 200 normally; 503 when no provider is configured or every provider failed.
        `exclude` leaves those providers out (used when covering for a provider that has just failed)."""
        started = time.perf_counter()
        max_results = min(request.max_results, self.settings.max_results)
        cache_key = "search:" + self._cache_key(request, max_results) + (":without:" + ",".join(sorted(exclude)) if exclude else "")
        errors: list[ErrorItem] = []
        cached = await self.cache.get(cache_key)
        from_cache = cached is not None
        status = 200

        if cached is not None:
            items = [ResultItem(**data) for data in cached["results"]]
            providers_used: list[str] = list(cached.get("providers_used", []))
        else:
            params = SearchParams(
                query=request.query, max_results=max_results, freshness=request.freshness, language=request.language,
                domains=tuple(request.domains), exclude_domains=tuple(request.exclude_domains),
            )  # fmt: skip
            outcome = await self.manager.search(params, mode=request.mode, provider=request.provider, exclude=exclude)
            errors = list(outcome.errors)
            if outcome.candidates == 0:
                errors = [ErrorItem(provider="*", error="no_providers_configured", detail="No search provider is configured. Set SEARXNG_URL, BRAVE_API_KEY, BING_API_KEY, GOOGLE_API_KEY + GOOGLE_CX, or ENABLE_WIKIPEDIA=true.")]
            items = self._build_items(outcome, request, max_results)
            providers_used = outcome.used
            if items and self.manager.all_cacheable(providers_used):  # Google's terms: no cached copies of its results
                await self.cache.set(cache_key, {"results": [i.model_dump(exclude=_CONTENT_FIELDS) for i in items], "providers_used": providers_used}, self.settings.cache_ttl_seconds)
            if not items and (outcome.candidates == 0 or not outcome.answered):
                status = 503  # nothing could answer (not merely "no results")

        if request.fetch_content and items:
            await self._attach_content(items)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response = SearchResponse(
            query=request.query,
            results=items,
            metadata=Metadata(
                provider=",".join(providers_used) or None, providers_used=providers_used, mode=request.mode, result_count=len(items),
                processing_time_ms=elapsed_ms, cached=from_cache, fetch_content=request.fetch_content, freshness=request.freshness, request_id=request_id,
            ),
            errors=errors,
        )  # fmt: skip
        outcome_label = "cached" if from_cache else "error" if status != 200 else "ok" if items else "empty"
        SEARCH_REQUESTS.labels(request.mode, outcome_label).inc()
        SEARCH_DURATION.labels(request.mode).observe(elapsed_ms / 1000)
        log_event(
            log, "search", query=request.query, mode=request.mode, provider=response.metadata.provider, results=len(items),
            cached=from_cache, fetch_content=request.fetch_content, errors=[e.provider + ":" + e.error for e in errors], duration_ms=elapsed_ms,
        )  # fmt: skip
        return response, status

    # ------------------------------------------------------------------ pipeline steps

    @staticmethod
    def _cache_key(request: SearchRequest, max_results: int) -> str:
        canonical = {
            "q": request.query.lower(), "n": max_results, "mode": request.mode, "fresh": request.freshness,
            "domains": sorted(request.domains), "exclude": sorted(request.exclude_domains), "lang": request.language, "provider": request.provider,
        }  # fmt: skip
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _passes_domain_filters(url: str, request: SearchRequest) -> bool:
        host = hostname_of(url)
        if request.domains and not any(domain_matches(host, d) for d in request.domains):
            return False
        return not any(domain_matches(host, d) for d in request.exclude_domains)

    def _build_items(self, outcome: ManagerOutcome, request: SearchRequest, max_results: int) -> list[ResultItem]:
        # Domain filters are enforced here for EVERY provider (providers that understand site: operators also apply them upstream,
        # but that is only a hint: the guarantee is this filter).
        results = [r for r in outcome.results if self._passes_domain_filters(r.url, request)]
        merged = merge_results(results)
        ranked = rank_results(merged, request.query, freshness=request.freshness, providers_answered=max(1, len(outcome.used)))
        return [
            ResultItem(
                rank=position, title=item.title, url=item.url, source=item.source or hostname_of(item.url) or None, snippet=item.snippet,
                published_at=item.published_at, providers=item.providers, score=score,
            )
            for position, (item, score) in enumerate(ranked[:max_results], start=1)
        ]  # fmt: skip

    async def _attach_content(self, items: list[ResultItem]) -> None:
        """Fetch pages concurrently (bounded), each with its own timeout, all within an overall ceiling. Never raises."""
        semaphore = asyncio.Semaphore(max(1, self.settings.max_concurrent_fetches))

        async def one(item: ResultItem) -> None:
            async with semaphore:
                await self._fetch_one(item)

        tasks = [asyncio.create_task(one(item)) for item in items]
        _, pending = await asyncio.wait(tasks, timeout=self.settings.fetch_total_timeout_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for item, task in zip(items, tasks):
            if task in pending and item.content is None and item.content_error is None:
                item.content_error = "timeout"

    async def _fetch_one(self, item: ResultItem) -> None:
        page_key = "page:" + dedup_key(item.url)
        entry = await self.cache.get(page_key)
        if entry is None:
            ttl = self.settings.page_cache_ttl_seconds
            try:
                page = await self.fetcher.fetch(item.url)
                # Extraction is pure-Python CPU work: running several at once in threads is SLOWER than one at a time (they fight over
                # the GIL: measured 11 s concurrent vs 3.7 s sequential for four pages). Page downloads stay parallel; extraction is
                # serialised, in a worker thread so the event loop stays free, under a timeout so one huge page cannot stall a request.
                async with self._extract_lock:
                    extracted = await asyncio.wait_for(
                        asyncio.to_thread(extract_content, page.text, page.url, self.settings.max_content_length, self.settings.max_extract_html_kb * 1024),
                        timeout=self.settings.request_timeout_seconds,
                    )
                if extracted.text:
                    entry = {"content": extracted.text, "content_error": None, "truncated": extracted.truncated, "published_at": extracted.published_at}
                else:
                    entry = {"content": None, "content_error": "no_extractable_content", "truncated": False, "published_at": None}
            except FetchError as exc:
                entry = {"content": None, "content_error": exc.code, "truncated": False, "published_at": None}
                ttl = min(ttl, 300)  # failures are cached briefly so a broken page is not retried on every request
            except asyncio.TimeoutError:
                entry = {"content": None, "content_error": "extraction_timeout", "truncated": False, "published_at": None}
                ttl = 300
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one odd page must never fail the whole request
                log_event(log, "page_error", level=logging.WARNING, url_host=hostname_of(item.url), error=type(exc).__name__)
                entry = {"content": None, "content_error": "fetch_failed", "truncated": False, "published_at": None}
                ttl = 60
            await self.cache.set(page_key, entry, ttl)
        item.content = entry.get("content")
        item.content_error = entry.get("content_error")
        item.content_truncated = bool(entry.get("truncated"))
        if item.published_at is None and entry.get("published_at"):
            item.published_at = entry["published_at"]
