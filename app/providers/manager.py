"""The provider manager: choose providers, run them, detect failure, fall back, report what happened.

FAST mode  tries providers in priority order until one returns results (a failed or empty provider is followed by the next).
DEEP mode  queries several providers in parallel; the caller merges, de-duplicates and ranks what comes back.
Providers that are cooling down (see health.py) are skipped and reported. The manager never raises for a provider failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, TypeVar

from app.config import Settings
from app.logging_config import log_event
from app.metrics import PROVIDER_DURATION, PROVIDER_ERRORS, PROVIDER_REQUESTS
from app.models.search import ErrorItem, SearchResult
from app.providers.base import ERROR_CLASSES, ProviderError, ProviderTimeout, ProviderUnavailable, SearchParams, SearchProvider
from app.providers.health import HealthTracker
from app.upstream import scrub

log = logging.getLogger("jonah.providers")
T = TypeVar("T")


class TrackedService(Protocol):
    """Anything whose calls are health-tracked: a SearchProvider, or an upstream API such as NewsAPI or Cloud Vision."""

    name: str

    def timeout_budget(self) -> float: ...


class UnknownProviderError(ValueError):
    """A specific provider was requested that does not exist or is not configured."""


@dataclass(slots=True)
class ManagerOutcome:
    results: list[SearchResult] = field(default_factory=list)
    used: list[str] = field(default_factory=list)  # providers that returned at least one result
    answered: list[str] = field(default_factory=list)  # providers that answered without error (even with zero results)
    errors: list[ErrorItem] = field(default_factory=list)
    candidates: int = 0  # providers that were eligible (configured), before cooldown skipping


class ProviderManager:
    def __init__(self, providers: list[SearchProvider], settings: Settings, health: HealthTracker | None = None) -> None:
        self.settings = settings
        self.health = health or HealthTracker()
        self._all = {p.name: p for p in providers}
        priority = settings.priority_list
        unknown = [name for name in priority if name not in self._all]
        if unknown:
            log_event(log, "unknown_providers_in_priority", level=logging.WARNING, names=unknown)
        # configured priority first, then any remaining providers in registration order
        ordered = [self._all[n] for n in priority if n in self._all]
        ordered += [p for p in providers if p not in ordered]
        self._ordered = ordered

    # ------------------------------------------------------------------ introspection

    @property
    def all_providers(self) -> list[SearchProvider]:
        return list(self._ordered)

    def enabled(self) -> list[SearchProvider]:
        return [p for p in self._ordered if p.is_configured()]

    def priority_of(self, name: str) -> int | None:
        names = [p.name for p in self._ordered]
        return names.index(name) + 1 if name in names else None

    def get(self, name: str) -> SearchProvider:
        provider = self._all.get(name)
        if provider is None:
            raise UnknownProviderError(f"unknown provider '{name}' (known: {', '.join(sorted(self._all))})")
        if not provider.is_configured():
            raise UnknownProviderError(f"provider '{name}' is not configured on this server")
        return provider

    # ------------------------------------------------------------------ searching

    def all_cacheable(self, names: list[str]) -> bool:
        """False if any of these providers' terms forbid caching their results."""
        return all(self._all[n].cacheable for n in names if n in self._all)

    async def search(self, params: SearchParams, *, mode: str = "fast", provider: str | None = None, exclude: tuple[str, ...] = ()) -> ManagerOutcome:
        """`exclude`: providers not to use this time (e.g. the one whose failure is being covered for)."""
        outcome = ManagerOutcome()
        if provider:
            candidates = [self.get(provider)]  # explicitly requested: try it even if it is cooling down
        else:
            candidates = [p for p in self.enabled() if p.name not in exclude]
        outcome.candidates = len(candidates)
        runnable: list[SearchProvider] = []
        for candidate in candidates:
            remaining = self.health.cooldown_remaining(candidate.name)
            if remaining > 0 and not provider:
                state = self.health.get(candidate.name)
                outcome.errors.append(ErrorItem(provider=candidate.name, error="cooling_down", detail=f"{state.cooldown_reason}; retry in about {int(remaining)}s"))
            else:
                runnable.append(candidate)
        if mode == "deep" and not provider:
            await self._deep(runnable[: max(1, self.settings.deep_mode_max_providers)], params, outcome)
        else:
            await self._fast(runnable, params, outcome)
        return outcome

    async def _fast(self, providers: list[SearchProvider], params: SearchParams, outcome: ManagerOutcome) -> None:
        for candidate in providers:
            results = await self._call(candidate, params, outcome)
            if results:
                outcome.results = results
                outcome.used = [candidate.name]
                return
        # no provider produced results: outcome.errors says which failed; providers that answered empty are in outcome.answered

    async def _deep(self, providers: list[SearchProvider], params: SearchParams, outcome: ManagerOutcome) -> None:
        if not providers:
            return
        batches = await asyncio.gather(*(self._call(p, params, outcome) for p in providers))
        for candidate, results in zip(providers, batches):
            if results:
                outcome.results.extend(results)
                outcome.used.append(candidate.name)

    def provider(self, name: str) -> SearchProvider | None:
        """The provider object by name (configured or not), or None."""
        return self._all.get(name)

    def cooling_down_error(self, name: str) -> ProviderError | None:
        """If `name` is cooling down: an error describing why (the last upstream message and status), so a caller can answer at once
        instead of spending another request on a provider that just said no. Otherwise None."""
        remaining = self.health.cooldown_remaining(name)
        if remaining <= 0:
            return None
        state = self.health.get(name)
        error_class = next((cls for cls in ERROR_CLASSES if cls.code == state.cooldown_reason), ProviderUnavailable)
        detail = state.last_error_detail or state.cooldown_reason or "recent failures"
        return error_class(f"{detail} (not retried for about {int(remaining)} s)", upstream_status=state.last_upstream_status)

    async def run_direct(self, service: TrackedService, call: Callable[[], Awaitable[T]]) -> T:
        """Run one provider-specific call (e.g. Google image search, NewsAPI) with the same time budget, health tracking, cool-downs
        and metrics as a search. Raises ProviderError (a timeout becomes ProviderTimeout)."""
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(call(), timeout=service.timeout_budget() + 1.0)
        except asyncio.TimeoutError:
            error = ProviderTimeout("timed out")
            self._note_failure(service, error, started)
            raise error from None
        except ProviderError as exc:
            self._note_failure(service, exc, started)
            raise
        self._note_success(service, started)
        return result

    def _note_success(self, service: TrackedService, started: float) -> float:
        elapsed = (time.perf_counter() - started) * 1000
        self.health.record_success(service.name, elapsed)
        PROVIDER_REQUESTS.labels(service.name, "ok").inc()
        PROVIDER_DURATION.labels(service.name).observe(elapsed / 1000)
        return elapsed

    def _note_failure(self, service: TrackedService, error: ProviderError, started: float) -> None:
        # Clean the upstream's message once, here, before it is stored or shown anywhere (errors[], /providers, cool-down messages).
        error.message = scrub(error.message, self.settings) if error.message else error.message
        elapsed = (time.perf_counter() - started) * 1000
        self.health.record_failure(service.name, error, elapsed)
        PROVIDER_REQUESTS.labels(service.name, "error").inc()
        PROVIDER_ERRORS.labels(service.name, error.code).inc()
        PROVIDER_DURATION.labels(service.name).observe(elapsed / 1000)
        log_event(log, "provider_error", level=logging.WARNING, provider=service.name, error=error.code, detail=error.message, duration_ms=int(elapsed))

    async def _call(self, provider: SearchProvider, params: SearchParams, outcome: ManagerOutcome) -> list[SearchResult]:
        started = time.perf_counter()
        # providers that cannot apply a filter on their own (no site: operators) are post-filtered by the service; ask for extra
        # results when filters are present so filtering does not leave the answer empty
        wanted = min(provider.max_per_request, params.max_results * (2 if (params.domains or params.exclude_domains) else 1))
        try:
            results = await asyncio.wait_for(
                provider.search(
                    params.query, wanted, freshness=params.freshness, language=params.language,
                    domains=params.domains if provider.supports_site_operators else (),
                    exclude_domains=params.exclude_domains if provider.supports_site_operators else (),
                ),
                timeout=provider.timeout_budget() + 1.0,
            )  # fmt: skip
        except asyncio.TimeoutError:
            return self._failed(provider, ProviderTimeout("timed out"), started, outcome)
        except ProviderError as exc:
            return self._failed(provider, exc, started, outcome)
        except Exception as exc:  # noqa: BLE001 - a bug in one provider must never take the request down
            log_event(log, "provider_bug", level=logging.ERROR, provider=provider.name, error=type(exc).__name__)
            return self._failed(provider, ProviderError(type(exc).__name__), started, outcome)
        elapsed = self._note_success(provider, started)
        outcome.answered.append(provider.name)
        log_event(log, "provider_ok", provider=provider.name, results=len(results), duration_ms=int(elapsed))
        return results

    def _failed(self, provider: SearchProvider, error: ProviderError, started: float, outcome: ManagerOutcome) -> list[SearchResult]:
        self._note_failure(provider, error, started)
        outcome.errors.append(ErrorItem(provider=provider.name, error=error.code, detail=(error.message or None)))
        return []
