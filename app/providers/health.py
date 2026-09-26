"""Provider health: a circuit breaker per provider, so a failing or exhausted provider is skipped instead of being hammered.

  quota_exceeded  -> skipped for an hour (a daily quota does not come back sooner)
  auth_failed     -> skipped for 10 minutes (a wrong key stays wrong)
  rate_limited    -> skipped for Retry-After (default 60 s)
  anything else   -> after 3 failures in a row: 30 s, doubling to a 5 minute ceiling
One success clears everything. Skipped providers are reported in the response's `errors` as "cooling_down", never silently dropped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from app.providers.base import ProviderError

QUOTA_COOLDOWN = 3600.0
AUTH_COOLDOWN = 600.0
RATE_LIMIT_DEFAULT = 60.0
FAILURE_THRESHOLD = 3
BASE_COOLDOWN = 30.0
MAX_COOLDOWN = 300.0


@dataclass(slots=True)
class ProviderHealth:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str | None = None
    requests: int = 0
    failures: int = 0
    last_error: str | None = None
    last_error_detail: str | None = None  # the upstream's own message (already free of keys)
    last_upstream_status: int | None = None
    last_latency_ms: float | None = None


class HealthTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state: dict[str, ProviderHealth] = {}

    def get(self, name: str) -> ProviderHealth:
        return self._state.setdefault(name, ProviderHealth())

    def cooldown_remaining(self, name: str) -> float:
        return max(0.0, self.get(name).cooldown_until - self._clock())

    def record_success(self, name: str, latency_ms: float) -> None:
        state = self.get(name)
        state.requests += 1
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.cooldown_reason = None
        state.last_latency_ms = latency_ms

    def record_failure(self, name: str, error: ProviderError, latency_ms: float) -> None:
        state = self.get(name)
        state.requests += 1
        state.failures += 1
        state.consecutive_failures += 1
        state.last_error = error.code
        state.last_error_detail = error.message or None
        state.last_upstream_status = error.upstream_status
        state.last_latency_ms = latency_ms
        now = self._clock()
        if error.code == "quota_exceeded":
            wait = error.retry_after or QUOTA_COOLDOWN
        elif error.code == "auth_failed":
            wait = AUTH_COOLDOWN
        elif error.code == "rate_limited":
            wait = error.retry_after or RATE_LIMIT_DEFAULT
        elif state.consecutive_failures >= FAILURE_THRESHOLD:
            wait = min(BASE_COOLDOWN * 2 ** (state.consecutive_failures - FAILURE_THRESHOLD), MAX_COOLDOWN)
        else:
            return
        state.cooldown_until = now + min(wait, QUOTA_COOLDOWN)
        state.cooldown_reason = error.code
