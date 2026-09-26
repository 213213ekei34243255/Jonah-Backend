"""Rate limiting: N requests per minute per client. In-memory (sliding window) by default; Redis (fixed window) when REDIS_URL is set,
so the limit holds across several workers/instances. A Redis outage fails OPEN (requests are allowed): rate limiting is protection, not
a reason to take the API down.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Callable

from app.config import Settings

log = logging.getLogger("jonah.ratelimit")
WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0


class RateLimiter(ABC):
    @abstractmethod
    async def hit(self, client_id: str) -> LimitDecision: ...

    async def close(self) -> None:  # pragma: no cover
        return None


class NullRateLimiter(RateLimiter):
    async def hit(self, client_id: str) -> LimitDecision:
        return LimitDecision(True, 0, 0)


class MemoryRateLimiter(RateLimiter):
    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    async def hit(self, client_id: str) -> LimitDecision:
        now = self._clock()
        self._calls += 1
        if self._calls % 500 == 0:  # forget clients that have gone quiet (before looking up this client's window)
            for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - WINDOW_SECONDS]:
                del self._hits[key]
        window = self._hits.setdefault(client_id, deque())
        while window and window[0] <= now - WINDOW_SECONDS:
            window.popleft()
        if len(window) >= self._limit:
            retry = int(WINDOW_SECONDS - (now - window[0])) + 1
            return LimitDecision(False, self._limit, 0, max(1, retry))
        window.append(now)
        return LimitDecision(True, self._limit, self._limit - len(window))


class RedisRateLimiter(RateLimiter):
    def __init__(self, client, limit: int, prefix: str = "jonah-search:rl:", wall: Callable[[], float] = time.time) -> None:
        self._redis = client
        self._limit = limit
        self._prefix = prefix
        self._wall = wall

    async def hit(self, client_id: str) -> LimitDecision:
        now = self._wall()
        bucket = int(now // WINDOW_SECONDS)
        key = f"{self._prefix}{client_id}:{bucket}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, WINDOW_SECONDS + 5)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis rate limiter unavailable (%s); allowing the request", type(exc).__name__)
            return LimitDecision(True, self._limit, self._limit)
        if count > self._limit:
            return LimitDecision(False, self._limit, 0, max(1, int(WINDOW_SECONDS - (now % WINDOW_SECONDS)) + 1))
        return LimitDecision(True, self._limit, self._limit - count)

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            return None


async def build_rate_limiter(settings: Settings) -> RateLimiter:
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return NullRateLimiter()
    if settings.redis_url:
        try:
            import redis.asyncio as redis_asyncio

            client = redis_asyncio.from_url(settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            await client.ping()
            log.info("rate limiter backend: redis")
            return RedisRateLimiter(client, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("REDIS_URL is set but Redis is not usable (%s); rate limiting in memory instead", type(exc).__name__)
    return MemoryRateLimiter(limit)
