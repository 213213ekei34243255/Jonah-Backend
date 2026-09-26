"""Redis cache (optional). Any Redis error is logged and treated as a cache miss: the API keeps working without it."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.cache.base import CacheBackend

log = logging.getLogger("jonah.cache")


class RedisCache(CacheBackend):
    name = "redis"

    def __init__(self, client, prefix: str = "jonah-search:cache:") -> None:
        self._redis = client
        self._prefix = prefix

    @classmethod
    async def connect(cls, url: str) -> "RedisCache":
        """Connect and ping. Raises if the `redis` package is missing or the server is unreachable (the caller falls back to memory)."""
        import redis.asyncio as redis_asyncio  # imported lazily: Redis is optional

        client = redis_asyncio.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30)
        await client.ping()
        return cls(client)

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(self._prefix + key)
            return None if raw is None else json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis cache get failed (%s); treating as a miss", type(exc).__name__)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            await self._redis.set(self._prefix + key, json.dumps(value, default=str), ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis cache set failed (%s); continuing without caching", type(exc).__name__)

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            return None
