"""robots.txt handling (RFC 9309), including the '*' and '$' wildcards the standard library's parser does not understand.

Rules: the most specific user-agent group wins (falling back to '*'); within a group the LONGEST matching pattern wins and Allow beats
Disallow on a tie; no matching rule means allowed. A robots.txt answered with 4xx means "no restrictions"; one that answers 5xx or cannot
be fetched means "do not crawl" (configurable with ROBOTS_FAIL_OPEN).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

# fetch(url) -> (http_status, body_text). May raise; any exception is treated as "robots.txt unavailable".
RobotsGetter = Callable[[str], Awaitable[tuple[int, str]]]

MAX_ROBOTS_BYTES = 500 * 1024  # RFC 9309: parse at least 500 KiB
_POSITIVE_TTL = 3600.0
_NEGATIVE_TTL = 300.0
_MAX_ORIGINS = 2000


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + regex + ("$" if anchored else ""))


@dataclass(slots=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


class RobotsRules:
    def __init__(self, groups: dict[str, list[_Rule]]) -> None:
        self._groups = groups

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        groups: dict[str, list[_Rule]] = {}
        current: list[str] = []
        reading_agents = False
        for raw_line in text[:MAX_ROBOTS_BYTES].splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if not reading_agents:
                    current = []
                    reading_agents = True
                token = value.lower()
                current.append(token)
                groups.setdefault(token, [])
            elif field in ("allow", "disallow"):
                reading_agents = False
                if not value:  # "Disallow:" (empty) means nothing is disallowed; an empty Allow adds nothing
                    continue
                rule = _Rule(field == "allow", value, _pattern_regex(value))
                for agent in current:
                    groups[agent].append(rule)
            else:
                reading_agents = False
        return cls(groups)

    def allowed(self, path_and_query: str, agent_token: str) -> bool:
        token = agent_token.lower()
        rules = None
        # most specific group: an agent line that is contained in our product token (e.g. 'jonahsearchbot'), longest first
        for name in sorted((n for n in self._groups if n != "*"), key=len, reverse=True):
            if name and name in token:
                rules = self._groups[name]
                break
        if rules is None:
            rules = self._groups.get("*", [])
        best: _Rule | None = None
        for rule in rules:
            if rule.regex.match(path_and_query):
                if best is None or len(rule.pattern) > len(best.pattern) or (len(rule.pattern) == len(best.pattern) and rule.allow):
                    best = rule
        return True if best is None else best.allow


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    reason: str | None = None  # 'robots_disallowed' | 'robots_unavailable' when not allowed


class RobotsChecker:
    def __init__(self, get: RobotsGetter, agent_token: str, *, fail_open: bool = False, clock: Callable[[], float] = time.monotonic) -> None:
        self._get = get
        self._agent = agent_token
        self._fail_open = fail_open
        self._clock = clock
        self._cache: dict[str, tuple[float, RobotsRules | str]] = {}  # origin -> (expires_at, rules | 'allow_all' | 'unavailable')
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str) -> RobotsDecision:
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        entry = await self._entry(origin)
        if entry == "unavailable":
            return RobotsDecision(True) if self._fail_open else RobotsDecision(False, "robots_unavailable")
        if entry == "allow_all":
            return RobotsDecision(True)
        path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        assert isinstance(entry, RobotsRules)
        return RobotsDecision(True) if entry.allowed(path_query, self._agent) else RobotsDecision(False, "robots_disallowed")

    async def _entry(self, origin: str) -> RobotsRules | str:
        cached = self._cache.get(origin)
        if cached and cached[0] > self._clock():
            return cached[1]
        # one download per origin even when several pages of it are requested at the same moment
        async with self._locks.setdefault(origin, asyncio.Lock()):
            cached = self._cache.get(origin)
            if cached and cached[0] > self._clock():
                return cached[1]
            entry: RobotsRules | str
            ttl = _POSITIVE_TTL
            try:
                status, body = await self._get(origin + "/robots.txt")
            except Exception:  # noqa: BLE001 - any failure (network, blocked address, timeout) means "cannot read robots.txt"
                entry, ttl = "unavailable", _NEGATIVE_TTL
            else:
                if 200 <= status < 300:
                    entry = RobotsRules.parse(body)
                elif status == 429:
                    entry, ttl = "unavailable", _NEGATIVE_TTL  # the site is asking us to slow down: not a licence to crawl
                elif 400 <= status < 500:
                    entry = "allow_all"  # RFC 9309: a client error means the file does not exist / no restrictions
                else:
                    entry, ttl = "unavailable", _NEGATIVE_TTL
            if len(self._cache) >= _MAX_ORIGINS:
                self._cache.clear()
                self._locks.clear()
            self._cache[origin] = (self._clock() + ttl, entry)
            return entry
