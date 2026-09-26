"""In-process cache: TTL + LRU eviction. Fine for one worker and for development; use Redis to share a cache across instances."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any, Callable

from app.cache.base import CacheBackend


class MemoryCache(CacheBackend):
    name = "memory"

    def __init__(self, max_entries: int = 1000, clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max(1, max_entries)
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()  # key -> (expires_at, json)

    async def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at <= self._clock():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return json.loads(payload)  # a fresh copy each time: callers cannot mutate what is cached

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            payload = json.dumps(value, default=str)
        except (TypeError, ValueError):
            return
        self._data[key] = (self._clock() + ttl_seconds, payload)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
