"""Cache factory: Redis when REDIS_URL is set and reachable, otherwise an in-memory cache."""

from __future__ import annotations

import logging

from app.cache.base import CacheBackend
from app.cache.memory import MemoryCache
from app.config import Settings

log = logging.getLogger("jonah.cache")

__all__ = ["CacheBackend", "MemoryCache", "build_cache"]


async def build_cache(settings: Settings) -> CacheBackend:
    if settings.redis_url:
        try:
            from app.cache.redis import RedisCache

            cache = await RedisCache.connect(settings.redis_url)
            log.info("cache backend: redis")
            return cache
        except Exception as exc:  # noqa: BLE001 - missing package, wrong URL, server down: fall back rather than fail to start
            log.warning("REDIS_URL is set but Redis is not usable (%s); using the in-memory cache instead", type(exc).__name__)
    return MemoryCache(settings.cache_max_entries)
