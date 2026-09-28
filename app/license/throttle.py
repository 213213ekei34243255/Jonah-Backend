"""In-memory brute-force protection. The service runs as ONE worker (WEB_CONCURRENCY=1, one SQLite file), so process memory is enough."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable


class FailureTracker:
    """Counts consecutive failures per key. After `free` failures the key is locked for `base_ms`, doubling with every further failure up to `max_ms`."""

    def __init__(self, free: int = 5, base_ms: int = 30_000, max_ms: int = 900_000, max_keys: int = 20_000, now_ms: Callable[[], float] | None = None) -> None:
        self.free, self.base_ms, self.max_ms, self.max_keys = free, base_ms, max_ms, max_keys
        self._now = now_ms or (lambda: time.time() * 1000)
        self._map: OrderedDict[str, list] = OrderedDict()  # key -> [count, locked_until_ms]
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """(locked, retry_after_ms)"""
        with self._lock:
            entry = self._map.get(key)
            if not entry:
                return False, 0
            left = entry[1] - self._now()
            return (True, int(left)) if left > 0 else (False, 0)

    def fail(self, key: str) -> None:
        with self._lock:
            entry = self._map.pop(key, None)
            if entry is None:
                if len(self._map) >= self.max_keys:
                    self._map.popitem(last=False)  # bounded: drop the oldest
                entry = [0, 0]
            entry[0] += 1
            if entry[0] >= self.free:
                entry[1] = self._now() + min(self.max_ms, self.base_ms * 2 ** (entry[0] - self.free))
            self._map[key] = entry  # most recent last

    def success(self, key: str) -> None:
        with self._lock:
            self._map.pop(key, None)


class SlidingWindow:
    """At most `limit` events per `window_ms` per key."""

    def __init__(self, limit: int, window_ms: int, max_keys: int = 20_000, now_ms: Callable[[], float] | None = None) -> None:
        self.limit, self.window_ms, self.max_keys = limit, window_ms, max_keys
        self._now = now_ms or (lambda: time.time() * 1000)
        self._map: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        with self._lock:
            t = self._now()
            events = self._map.get(key)
            if events is None:
                if len(self._map) >= self.max_keys:
                    self._map.popitem(last=False)
                events = deque()
                self._map[key] = events
            while events and events[0] <= t - self.window_ms:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(t)
            return True
