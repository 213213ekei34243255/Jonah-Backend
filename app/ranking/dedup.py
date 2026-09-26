"""URL normalisation and de-duplication.

Two URLs are "the same page" when they differ only in things that do not change the page: scheme (http/https), a leading 'www.', host
case, default ports, a trailing slash, the #fragment, the ORDER of query parameters, and well-known tracking parameters. Every other query
parameter is kept: '?id=7' and '?id=8' are different pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.models.search import SearchResult

TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "utm_name", "utm_brand",
        "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid", "yclid", "twclid", "ttclid", "li_fat_id",
        "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "mkt_tok", "vero_id", "oly_enc_id", "oly_anon_id", "ref_src", "ref_url",
    }
)  # fmt: skip
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _clean_query(query: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]


def normalize_url(url: str) -> str:
    """A tidy, still-valid URL: lower-case scheme and host, default port and #fragment removed, tracking parameters removed
    (other parameters kept, in their original order), empty path -> '/'. The path itself (including a trailing slash) is not altered."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url.strip()
    if not parts.scheme or not host:
        return url.strip()
    scheme = parts.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port and port != _DEFAULT_PORTS.get(scheme):
        netloc += f":{port}"
    query = urlencode(_clean_query(parts.query), quote_via=quote)
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def dedup_key(url: str) -> str:
    """Equal for URLs that are the same page (see module docstring). Only used for comparison, never shown."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
        port = parts.port
    except ValueError:
        return url.strip().lower()
    scheme = parts.scheme.lower()
    port_part = f":{port}" if port and port != _DEFAULT_PORTS.get(scheme) else ""
    path = parts.path.rstrip("/") or ""
    query = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(_clean_query(parts.query)))
    return f"{host}{port_part}{path}{'?' + query if query else ''}"


@dataclass(slots=True)
class MergedResult:
    """One page, as reported by one or more providers."""

    title: str
    url: str  # normalised
    snippet: str | None = None
    source: str | None = None
    published_at: str | None = None
    provider_ranks: dict[str, int] = field(default_factory=dict)  # provider -> best (lowest) rank it gave this page

    @property
    def providers(self) -> list[str]:
        return sorted(self.provider_ranks, key=lambda p: (self.provider_ranks[p], p))

    @property
    def best_rank(self) -> int:
        return min(self.provider_ranks.values()) if self.provider_ranks else 10**6


def merge_results(results: list[SearchResult]) -> list[MergedResult]:
    """Group results that are the same page. The kept title/snippet/date come from the best-ranked provider entry, filling gaps
    (a missing snippet, a longer snippet, a missing date) from the others."""
    merged: dict[str, MergedResult] = {}
    for result in sorted(results, key=lambda r: (r.rank, r.provider)):
        key = dedup_key(result.url)
        current = merged.get(key)
        if current is None:
            merged[key] = MergedResult(
                title=result.title, url=normalize_url(result.url), snippet=result.snippet, source=result.source,
                published_at=result.published_at, provider_ranks={result.provider: result.rank},
            )  # fmt: skip
            continue
        current.provider_ranks[result.provider] = min(result.rank, current.provider_ranks.get(result.provider, result.rank))
        if result.snippet and (not current.snippet or len(result.snippet) > len(current.snippet) * 1.5):
            current.snippet = result.snippet
        current.published_at = current.published_at or result.published_at
        current.source = current.source or result.source
    return list(merged.values())
