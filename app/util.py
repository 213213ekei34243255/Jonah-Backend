"""Small pure helpers shared by providers, ranking and the scraper."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

_RELATIVE = re.compile(r"^\s*(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def parse_datetime(value, now: datetime | None = None) -> str | None:
    """Any date a provider might give us -> ISO 8601 UTC ('2026-09-26T10:00:00Z'), or None when it cannot be understood."""
    if value is None or value == "":
        return None
    now = now or datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value  # milliseconds vs seconds
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    relative = _RELATIVE.match(text)
    if relative:
        return (now - timedelta(seconds=int(relative.group(1)) * _UNIT_SECONDS[relative.group(2).lower()])).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hostname_of(url: str) -> str:
    """Lower-case host without a leading 'www.' ('' when the URL has none)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domain: str) -> bool:
    """True when `host` is `domain` or a subdomain of it (both without 'www.')."""
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return bool(host) and (host == domain or host.endswith("." + domain))


def strip_tags(text: str | None) -> str:
    """Provider snippets sometimes carry markup ('<b>match</b> &amp; more'): plain text only."""
    if not text:
        return ""
    return _SPACE.sub(" ", html.unescape(_TAGS.sub("", text))).strip()
