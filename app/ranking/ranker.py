"""Deterministic, transparent ranking (no machine learning).

score = 0.50 * rank_fusion + 0.15 * agreement + 0.15 * title_match + 0.10 * snippet_match + 0.05 * domain_quality + 0.05 * recency

  rank_fusion   Reciprocal Rank Fusion over the providers that returned the page: sum(1 / (60 + rank)), scaled so that a page ranked
                first by every provider scores 1.0. Rewards high positions, and positions agreed on by several providers.
  agreement     Fraction of the providers that returned the page (1.0 = every provider that answered returned it).
  title_match   Fraction of the query's meaningful words that appear in the title.
  snippet_match Fraction of the query's meaningful words that appear in the snippet.
  domain_quality  +1.0 for .gov/.edu/.ac.* and a short list of reference/documentation domains, 0.5 for a neutral domain, 0.0 for
                  a short list of content-farm style domains. (Small, explicit and easy to change: see HIGH_QUALITY / LOW_QUALITY.)
  recency       exp(-age_days / 30) when a publication date is known; 0.3 when unknown. A light tiebreaker by default (an old page can
                be the best answer to an evergreen question), but when the request asked for fresh results (freshness != any) its
                weight rises from 0.05 to 0.20 (the 0.15 comes out of rank_fusion).

Ties break on the best provider rank, then the URL, so the order is fully reproducible.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from app.ranking.dedup import MergedResult
from app.util import domain_matches, hostname_of

RRF_K = 60
WEIGHTS = {"fusion": 0.50, "agreement": 0.15, "title": 0.15, "snippet": 0.10, "domain": 0.05, "recency": 0.05}
FRESH_RECENCY_BOOST = 0.15  # moved from `fusion` to `recency` when the request asked for fresh results

HIGH_QUALITY = (
    "wikipedia.org", "britannica.com", "arxiv.org", "nature.com", "science.org", "nih.gov", "who.int", "github.com", "stackoverflow.com",
    "developer.mozilla.org", "docs.python.org", "w3.org", "ietf.org", "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
)  # fmt: skip
LOW_QUALITY = ("pinterest.com", "quora.com", "ehow.com", "answers.com", "ask.com", "w3schools.in")
_HIGH_TLD = re.compile(r"\.(gov|edu|mil)(\.[a-z]{2})?$|\.ac\.[a-z]{2}$|\.gov\.[a-z]{2}$")

_STOPWORDS = frozenset(
    "a an and are as at be but by for from how i in is it its of on or that the this to was what when where which who why will with you your".split()
)
_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def query_terms(query: str) -> list[str]:
    terms = [w for w in _WORD.findall(query.lower()) if w not in _STOPWORDS and len(w) > 1]
    return terms or [w for w in _WORD.findall(query.lower())]


def _coverage(terms: list[str], text: str | None) -> float:
    if not terms or not text:
        return 0.0
    words = set(_WORD.findall(text.lower()))
    return sum(1 for t in terms if t in words or any(w.startswith(t) for w in words)) / len(terms)


def domain_quality(url: str) -> float:
    host = hostname_of(url)
    if not host:
        return 0.5
    if _HIGH_TLD.search(host) or any(domain_matches(host, d) for d in HIGH_QUALITY):
        return 1.0
    if any(domain_matches(host, d) for d in LOW_QUALITY):
        return 0.0
    return 0.5


def recency(published_at: str | None, now: datetime) -> float:
    if not published_at:
        return 0.3
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.3
    age_days = max(0.0, (now - published).total_seconds() / 86400)
    return math.exp(-age_days / 30)


def rank_results(
    merged: list[MergedResult], query: str, *, freshness: str = "any", providers_answered: int = 1, now: datetime | None = None
) -> list[tuple[MergedResult, float]]:
    """Score and order merged results. Returns [(result, score)] best first."""
    now = now or datetime.now(timezone.utc)
    terms = query_terms(query)
    weights = dict(WEIGHTS)
    if freshness != "any":
        weights["fusion"] -= FRESH_RECENCY_BOOST
        weights["recency"] += FRESH_RECENCY_BOOST
    best_possible = max(1, providers_answered) / (RRF_K + 1)  # every answering provider ranked it first
    scored: list[tuple[MergedResult, float]] = []
    for item in merged:
        fusion = sum(1.0 / (RRF_K + rank) for rank in item.provider_ranks.values()) / best_possible
        agreement = len(item.provider_ranks) / max(1, providers_answered)
        score = (
            weights["fusion"] * min(1.0, fusion)
            + weights["agreement"] * min(1.0, agreement)
            + weights["title"] * _coverage(terms, item.title)
            + weights["snippet"] * _coverage(terms, item.snippet)
            + weights["domain"] * domain_quality(item.url)
            + weights["recency"] * recency(item.published_at, now)
        )
        scored.append((item, round(score, 4)))
    scored.sort(key=lambda pair: (-pair[1], pair[0].best_rank, pair[0].url))
    return scored
