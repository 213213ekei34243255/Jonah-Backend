"""Cache interface. Values are JSON-serialisable; a cache failure is never an API failure (a miss is always a safe answer)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CacheBackend(ABC):
    name: str = "base"

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """The cached value, or None on a miss / expiry / backend error."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store a value. Never raises."""

    async def close(self) -> None:  # pragma: no cover - default is nothing to release
        return None
