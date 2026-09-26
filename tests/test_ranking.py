"""URL normalisation, de-duplication and ranking."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ranking.dedup import dedup_key, merge_results, normalize_url
from app.ranking.ranker import domain_quality, query_terms, rank_results, recency
from tests.conftest import result

# ----------------------------------------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("HTTPS://Example.COM/Path?a=1", "https://example.com/Path?a=1"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:80/x", "http://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://example.com/x#section-2", "https://example.com/x"),
        ("https://example.com/x?utm_source=a&utm_medium=b&id=7", "https://example.com/x?id=7"),
        ("https://example.com/x?gclid=abc&fbclid=def", "https://example.com/x"),
        ("https://example.com/x?utm_campaign=z&b=2&a=1", "https://example.com/x?b=2&a=1"),  # legitimate params kept, original order
        ("https://example.com/x/", "https://example.com/x/"),  # the path itself is not rewritten
    ],
)
def test_normalize_url(raw, clean):
    assert normalize_url(raw) == clean


def test_legitimate_query_parameters_are_never_dropped():
    assert normalize_url("https://example.com/watch?v=abc123&t=42s") == "https://example.com/watch?v=abc123&t=42s"
    assert normalize_url("https://example.com/search?q=a+b&page=2") == "https://example.com/search?q=a+b&page=2".replace("a+b", "a%20b")
    assert dedup_key("https://example.com/p?id=7") != dedup_key("https://example.com/p?id=8")


def test_different_forms_of_the_same_url_share_one_key():
    same = [
        "https://example.com/article",
        "http://example.com/article",  # scheme
        "https://www.example.com/article",  # www
        "https://EXAMPLE.com/article/",  # case + trailing slash
        "https://example.com/article#comments",  # fragment
        "https://example.com:443/article",  # default port
        "https://example.com/article?utm_source=newsletter&fbclid=xyz",  # tracking
    ]
    assert len({dedup_key(u) for u in same}) == 1


def test_parameter_order_does_not_change_the_key_but_values_do():
    assert dedup_key("https://e.com/x?a=1&b=2") == dedup_key("https://e.com/x?b=2&a=1")
    assert dedup_key("https://e.com/x?a=1") != dedup_key("https://e.com/x?a=2")
    assert dedup_key("https://e.com/x") != dedup_key("https://e.com/y")
    assert dedup_key("https://a.example.com/x") != dedup_key("https://example.com/x")  # a real subdomain is a different site


def test_malformed_urls_do_not_crash():
    assert normalize_url("not a url") == "not a url"
    assert dedup_key("http://[bad") == "http://[bad"


# ----------------------------------------------------------------------------------------------------- merging


def test_merge_combines_the_same_page_from_several_providers():
    merged = merge_results(
        [
            result("Page", "https://example.com/a?utm_source=x", rank=2, provider="searxng", snippet="short"),
            result("Page", "https://www.example.com/a", rank=1, provider="brave", snippet="a much longer and more informative snippet for the page"),
            result("Other", "https://other.org/", rank=1, provider="searxng"),
        ]
    )
    assert len(merged) == 2
    page = next(m for m in merged if "example.com" in m.url)
    assert page.provider_ranks == {"searxng": 2, "brave": 1}
    assert page.providers == ["brave", "searxng"]
    assert page.snippet.startswith("a much longer")  # the more informative snippet wins
    assert "utm_source" not in page.url


def test_merge_fills_gaps_from_other_providers():
    merged = merge_results(
        [result("T", "https://e.com/a", rank=1, provider="a", snippet=None), result("T", "https://e.com/a", rank=3, provider="b", snippet="found here", published_at="2026-01-01T00:00:00Z")]
    )
    assert merged[0].snippet == "found here"
    assert merged[0].published_at == "2026-01-01T00:00:00Z"


def test_merge_keeps_the_best_rank_per_provider():
    merged = merge_results([result("T", "https://e.com/a", rank=5, provider="a"), result("T", "https://e.com/a/", rank=2, provider="a")])
    assert merged[0].provider_ranks == {"a": 2}


# ---------------------------------------------------------------------------------------------------- ranking

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def ranked(results, query="python tutorial", **kw):
    return rank_results(merge_results(results), query, now=NOW, **kw)


def test_query_terms_drop_stopwords():
    assert query_terms("what is the best python tutorial") == ["best", "python", "tutorial"]
    assert query_terms("the and of") == ["the", "and", "of"]  # a query of only stopwords keeps its words


def test_higher_provider_rank_scores_higher():
    order = ranked([result("Python tutorial", "https://a.com/1", rank=1), result("Python tutorial", "https://b.com/2", rank=9)])
    assert [m.url for m, _ in order] == ["https://a.com/1", "https://b.com/2"]
    assert order[0][1] > order[1][1]


def test_a_page_returned_by_several_providers_beats_one_returned_by_one():
    results = [
        result("Python tutorial", "https://one.com/x", rank=1, provider="a"),
        result("Python tutorial", "https://both.com/x", rank=2, provider="a"),
        result("Python tutorial", "https://both.com/x", rank=2, provider="b"),
    ]
    order = ranked(results, providers_answered=2)
    assert order[0][0].url == "https://both.com/x"


def test_title_and_snippet_relevance_matters():
    results = [
        result("Cooking pasta at home", "https://a.com/1", rank=1, snippet="recipes"),
        result("Python tutorial for beginners", "https://b.com/2", rank=2, snippet="learn python step by step"),
    ]
    order = ranked(results)
    assert order[0][0].url == "https://b.com/2"


def test_fresh_pages_score_higher_and_matter_more_when_freshness_is_requested():
    old = result("Python tutorial", "https://old.com/1", rank=1, published_at="2020-01-01T00:00:00Z")
    new = result("Python tutorial", "https://new.com/2", rank=2, published_at="2026-09-25T00:00:00Z")
    assert recency("2026-09-26T00:00:00Z", NOW) > recency("2020-01-01T00:00:00Z", NOW)
    assert recency(None, NOW) == 0.3
    assert ranked([old, new], freshness="day")[0][0].url == "https://new.com/2"  # freshness requested: recency counts for a lot
    # freshness NOT requested: recency is only a light tiebreaker, so a page that is clearly better placed still wins
    much_better = result("Python tutorial", "https://old.com/1", rank=1, published_at="2020-01-01T00:00:00Z")
    buried = result("Python tutorial", "https://new.com/2", rank=15, published_at="2026-09-25T00:00:00Z")
    assert ranked([much_better, buried], freshness="any")[0][0].url == "https://old.com/1"
    assert ranked([much_better, buried], freshness="day")[0][0].url == "https://new.com/2"


def test_domain_quality_signals():
    assert domain_quality("https://www.nasa.gov/x") == 1.0
    assert domain_quality("https://cs.stanford.edu/x") == 1.0
    assert domain_quality("https://en.wikipedia.org/wiki/X") == 1.0
    assert domain_quality("https://www.pinterest.com/pin/1") == 0.0
    assert domain_quality("https://random-blog.example/x") == 0.5


def test_ranking_is_deterministic_and_ties_break_stably():
    same = [result("Python tutorial", "https://b.com/x", rank=1, provider="a"), result("Python tutorial", "https://a.com/x", rank=1, provider="b")]
    first = [m.url for m, _ in ranked(same, providers_answered=2)]
    second = [m.url for m, _ in ranked(list(reversed(same)), providers_answered=2)]
    assert first == second == ["https://a.com/x", "https://b.com/x"]  # equal scores: alphabetical URL


def test_scores_are_bounded():
    for _, score in ranked([result("Python tutorial", "https://a.com/1", rank=1, published_at="2026-09-26T00:00:00Z")]):
        assert 0 <= score <= 1.0
