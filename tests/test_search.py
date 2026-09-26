"""The HTTP API, end to end: a real app (lifespan, middleware, routes, service) driven in-process with fake providers and pages."""

from __future__ import annotations

import pytest

from app.cache.memory import MemoryCache
from app.providers.base import ProviderQuotaExceeded, ProviderTimeout, ProviderUnavailable
from tests.conftest import FakeFetcher, FakeProvider, make_settings, result

pytestmark = pytest.mark.anyio

KEY_1 = "k1-secret-abcdef"
KEY_2 = "k2-secret-ghijkl"


def web_results(provider: str = "fake") -> list:
    return [
        result("Python (programming language)", "https://en.wikipedia.org/wiki/Python_(programming_language)", 1, provider, "Python is a programming language."),
        result("Welcome to Python.org", "https://www.python.org/", 2, provider, "The official home of the Python programming language."),
        result("Python tutorial", "https://docs.python.org/3/tutorial/", 3, provider, "An informal introduction to Python."),
        result("Learn Python on arxiv", "https://arxiv.org/abs/1234.5678", 4, provider, "A paper about Python."),
    ]


# ------------------------------------------------------------------------------------------------ service info


async def test_root_health_and_docs(make_client):
    client, _ = await make_client()
    root = (await client.get("/")).json()
    assert root["name"] == "Jonah Search" and root["status"] == "ok" and root["version"] and root["docs"] == "/docs"
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/search" in schema["paths"] and {"get", "post"} <= set(schema["paths"]["/search"])


# ------------------------------------------------------------------------------------------------ searching


async def test_get_search_returns_the_documented_shape(make_client):
    provider = FakeProvider("searxng", web_results("searxng"))
    client, _ = await make_client(providers=[provider])
    response = await client.get("/search", params={"q": "python programming language", "max_results": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "python programming language" and body["errors"] == []
    assert len(body["results"]) == 3
    first = body["results"][0]
    assert set(first) >= {"rank", "title", "url", "source", "snippet", "published_at", "content", "content_error", "providers", "score"}
    assert [r["rank"] for r in body["results"]] == [1, 2, 3]
    assert first["providers"] == ["searxng"] and first["content"] is None
    meta = body["metadata"]
    assert meta["provider"] == "searxng" and meta["result_count"] == 3 and meta["cached"] is False and meta["mode"] == "fast"
    assert isinstance(meta["processing_time_ms"], int) and meta["content_trust"] == "untrusted"


async def test_post_search_accepts_json(make_client):
    provider = FakeProvider("brave", web_results("brave"))
    client, _ = await make_client(providers=[provider])
    response = await client.post("/search", json={"query": "python", "max_results": 2, "freshness": "week", "language": "en"})
    assert response.status_code == 200 and len(response.json()["results"]) == 2
    call = provider.calls[0]
    assert (call.query, call.freshness, call.language) == ("python", "week", "en")


async def test_the_query_is_normalised(make_client):
    provider = FakeProvider("a", web_results("a"))
    client, _ = await make_client(providers=[provider])
    body = (await client.post("/search", json={"query": "   python \n  tutorial  "})).json()
    assert body["query"] == "python tutorial" and provider.calls[0].query == "python tutorial"


async def test_max_results_is_capped_by_the_server(make_client):
    many = [result(f"Result {i}", f"https://site{i}.com/", i, "a") for i in range(1, 30)]
    client, _ = await make_client(make_settings(max_results=5), providers=[FakeProvider("a", many)])
    body = (await client.get("/search", params={"q": "result", "max_results": 50})).json()
    assert len(body["results"]) == 5


@pytest.mark.parametrize(
    "params",
    [
        {},  # no q at all
        {"q": ""},
        {"q": "   "},
        {"q": "x" * 501},
        {"q": "ok", "max_results": 0},
        {"q": "ok", "max_results": 101},
        {"q": "ok", "freshness": "decade"},
        {"q": "ok", "mode": "turbo"},
        {"q": "ok", "domains": "not a domain!"},
        {"q": "ok", "exclude_domains": "http://"},
        {"q": "ok", "language": "english please"},
        {"q": "ok", "fetch_content": "maybe"},
    ],
)
async def test_invalid_get_parameters_are_422(make_client, params):
    provider = FakeProvider("a", web_results("a"))
    client, _ = await make_client(providers=[provider])
    response = await client.get("/search", params=params)
    assert response.status_code == 422, response.text
    assert provider.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "ok", "unexpected_field": 1},
        {"query": "ok", "max_results": "ten"},
        {"query": "ok", "domains": ["a b c"]},
        {"query": "ok", "domains": [f"d{i}.com" for i in range(21)]},
        {"query": 123},
    ],
)
async def test_invalid_post_bodies_are_422(make_client, body):
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    assert (await client.post("/search", json=body)).status_code == 422


async def test_malformed_json_is_422(make_client):
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    response = await client.post("/search", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422


async def test_domain_filters_are_enforced_even_if_a_provider_ignores_them(make_client):
    provider = FakeProvider("a", web_results("a"))  # returns every domain regardless of the filter
    client, _ = await make_client(providers=[provider])
    only = (await client.get("/search", params={"q": "python", "domains": "python.org"})).json()["results"]
    assert {r["url"] for r in only} == {"https://www.python.org/", "https://docs.python.org/3/tutorial/"}  # subdomains included
    assert provider.calls[0].domains == ("python.org",)
    excluded = (await client.get("/search", params=[("q", "python"), ("exclude_domains", "python.org"), ("exclude_domains", "arxiv.org")])).json()["results"]
    assert [r["url"] for r in excluded] == ["https://en.wikipedia.org/wiki/Python_(programming_language)"]


async def test_domains_accept_urls_and_comma_lists(make_client):
    provider = FakeProvider("a", web_results("a"))
    client, _ = await make_client(providers=[provider])
    await client.get("/search", params={"q": "python", "domains": "https://www.Arxiv.org/abs, python.org"})
    assert provider.calls[0].domains == ("arxiv.org", "python.org")


# ------------------------------------------------------------------------------------------------ page content


async def test_fetch_content_returns_clean_text(make_client):
    fetcher = FakeFetcher()
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a")[:2])], fetcher=fetcher)
    body = (await client.get("/search", params={"q": "python", "fetch_content": "true"})).json()
    assert len(fetcher.calls) == 2 and body["metadata"]["fetch_content"] is True
    for item in body["results"]:
        assert item["content_error"] is None
        assert "search engine is a program" in item["content"]
        assert "SECRET_TRACKING_CODE" not in item["content"] and "<p>" not in item["content"]


async def test_fetch_content_off_never_downloads_pages(make_client):
    fetcher = FakeFetcher()
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))], fetcher=fetcher)
    body = (await client.get("/search", params={"q": "python"})).json()
    assert fetcher.calls == [] and all(r["content"] is None and r["content_error"] is None for r in body["results"])


async def test_a_page_that_cannot_be_fetched_gets_a_content_error_and_the_rest_still_work(make_client):
    blocked = "https://www.python.org/"
    fetcher = FakeFetcher(errors={blocked: "robots_disallowed"})
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a")[:2])], fetcher=fetcher)
    body = (await client.post("/search", json={"query": "python", "fetch_content": True})).json()
    by_url = {r["url"]: r for r in body["results"]}
    assert by_url[blocked]["content"] is None and by_url[blocked]["content_error"] == "robots_disallowed"
    other = by_url["https://en.wikipedia.org/wiki/Python_(programming_language)"]
    assert other["content"] and other["content_error"] is None


async def test_a_page_with_no_readable_text_is_reported(make_client):
    fetcher = FakeFetcher(default_html="<html><body><script>app()</script></body></html>")
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a")[:1])], fetcher=fetcher)
    item = (await client.post("/search", json={"query": "python", "fetch_content": True})).json()["results"][0]
    assert item["content"] is None and item["content_error"] == "no_extractable_content"


async def test_long_pages_are_truncated_and_flagged(make_client):
    html = "<html><body><article>" + "<p>" + ("Plenty of genuine article text about search. " * 60) + "</p>" * 1 + "</article></body></html>"
    client, _ = await make_client(make_settings(max_content_length=500), providers=[FakeProvider("a", web_results("a")[:1])], fetcher=FakeFetcher(default_html=html))
    item = (await client.post("/search", json={"query": "python", "fetch_content": True})).json()["results"][0]
    assert item["content_truncated"] is True and len(item["content"]) <= 500


# ------------------------------------------------------------------------------------------------ providers failing


async def test_fallback_is_reported_in_errors(make_client):
    providers = [FakeProvider("searxng", error=ProviderTimeout("timed out")), FakeProvider("brave", web_results("brave"))]
    client, _ = await make_client(make_settings(provider_priority="searxng,brave"), providers=providers)
    response = await client.get("/search", params={"q": "python"})
    body = response.json()
    assert response.status_code == 200 and body["metadata"]["provider"] == "brave"
    assert body["errors"] == [{"provider": "searxng", "error": "timeout", "detail": "timed out"}]


async def test_every_provider_failing_is_a_503_with_the_normal_shape(make_client):
    providers = [FakeProvider("searxng", error=ProviderUnavailable("HTTP 502")), FakeProvider("google", error=ProviderQuotaExceeded("Queries per day"))]
    client, _ = await make_client(make_settings(provider_priority="searxng,google"), providers=providers)
    response = await client.get("/search", params={"q": "python"})
    assert response.status_code == 503
    body = response.json()
    assert body["results"] == [] and body["metadata"]["result_count"] == 0
    assert [(e["provider"], e["error"]) for e in body["errors"]] == [("searxng", "unavailable"), ("google", "quota_exceeded")]


async def test_no_configured_provider_is_a_clear_503(make_client):
    client, _ = await make_client(providers=[FakeProvider("brave", configured=False)])
    response = await client.get("/search", params={"q": "python"})
    assert response.status_code == 503
    assert response.json()["errors"][0]["error"] == "no_providers_configured"


async def test_no_results_is_a_200_not_an_error(make_client):
    client, _ = await make_client(providers=[FakeProvider("a", [])])
    response = await client.get("/search", params={"q": "zxqvbnm"})
    assert response.status_code == 200 and response.json()["results"] == [] and response.json()["errors"] == []


async def test_forcing_a_provider(make_client):
    a, b = FakeProvider("a", web_results("a")), FakeProvider("b", web_results("b"))
    client, _ = await make_client(make_settings(provider_priority="a,b"), providers=[a, b])
    body = (await client.get("/search", params={"q": "python", "provider": "B"})).json()
    assert body["metadata"]["provider"] == "b" and a.calls == []
    unknown = await client.get("/search", params={"q": "python", "provider": "yahoo"})
    assert unknown.status_code == 400 and "yahoo" in unknown.json()["detail"]


async def test_deep_mode_merges_providers(make_client):
    a = FakeProvider("a", web_results("a"))
    b = FakeProvider("b", [result("Python.org", "https://python.org/?utm_source=b", 1, "b"), result("Only B", "https://b-only.com/", 2, "b")])
    client, _ = await make_client(make_settings(provider_priority="a,b"), providers=[a, b])
    body = (await client.post("/search", json={"query": "python", "mode": "deep", "max_results": 10})).json()
    assert body["metadata"]["mode"] == "deep" and sorted(body["metadata"]["providers_used"]) == ["a", "b"]
    assert body["metadata"]["provider"] in ("a,b", "b,a")
    urls = [r["url"] for r in body["results"]]
    assert len(urls) == 5  # python.org came back from both providers and appears once
    merged = next(r for r in body["results"] if "python.org" in r["url"] and "docs" not in r["url"])
    assert sorted(merged["providers"]) == ["a", "b"]
    assert body["results"][0] is not None and merged["rank"] == 1  # agreement between providers ranks it first


# ------------------------------------------------------------------------------------------------ cache


async def test_identical_searches_are_served_from_the_cache(make_client):
    provider = FakeProvider("a", web_results("a"))
    client, _ = await make_client(providers=[provider])
    first = (await client.get("/search", params={"q": "Python"})).json()
    second = (await client.get("/search", params={"q": "python"})).json()  # case does not matter
    assert len(provider.calls) == 1
    assert first["metadata"]["cached"] is False and second["metadata"]["cached"] is True
    assert [r["url"] for r in first["results"]] == [r["url"] for r in second["results"]]
    await client.get("/search", params={"q": "python", "freshness": "day"})  # different parameters: a new search
    assert len(provider.calls) == 2


async def test_failures_are_not_cached(make_client):
    flaky = FakeProvider("a", error=ProviderUnavailable("down"))
    client, _ = await make_client(providers=[flaky])
    assert (await client.get("/search", params={"q": "python"})).status_code == 503
    flaky.error, flaky.results = None, web_results("a")  # one failure does not cool a provider down (three in a row do)
    assert (await client.get("/search", params={"q": "python"})).status_code == 200


async def test_page_content_is_cached_separately(make_client):
    fetcher = FakeFetcher()
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a")[:2])], fetcher=fetcher, cache=MemoryCache())
    await client.get("/search", params={"q": "python", "fetch_content": "true"})
    await client.get("/search", params={"q": "python tutorial", "fetch_content": "true"})  # a different search, the same pages
    assert len(fetcher.calls) == 2


# ------------------------------------------------------------------------------------------------ authentication


async def test_authentication(make_client):
    settings = make_settings(search_api_key=f"{KEY_1}, {KEY_2}")
    client, _ = await make_client(settings, providers=[FakeProvider("a", web_results("a"))])
    missing = await client.get("/search", params={"q": "python"})
    assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
    for header in (f"Bearer {KEY_1}x", "Bearer ", f"Basic {KEY_1}", KEY_1, f"Bearer {KEY_1[:-1]}"):
        assert (await client.get("/search", params={"q": "python"}, headers={"Authorization": header})).status_code == 401, header
    for key in (KEY_1, KEY_2):
        assert (await client.get("/search", params={"q": "python"}, headers={"Authorization": f"Bearer {key}"})).status_code == 200
    assert (await client.post("/search", json={"query": "python"}, headers={"Authorization": f"bearer {KEY_2}"})).status_code == 200


async def test_health_and_info_stay_open_but_providers_and_metrics_are_protected(make_client):
    client, _ = await make_client(make_settings(search_api_key=KEY_1), providers=[FakeProvider("a", web_results("a"))])
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/providers")).status_code == 401
    assert (await client.get("/metrics")).status_code == 401
    auth = {"Authorization": f"Bearer {KEY_1}"}
    assert (await client.get("/providers", headers=auth)).status_code == 200
    assert (await client.get("/metrics", headers=auth)).status_code == 200


async def test_the_key_never_appears_in_a_response(make_client):
    client, _ = await make_client(make_settings(search_api_key=KEY_1), providers=[FakeProvider("a", web_results("a"))])
    for response in (
        await client.get("/search", params={"q": "python"}, headers={"Authorization": f"Bearer {KEY_1}"}),
        await client.get("/search", params={"q": "python"}, headers={"Authorization": f"Bearer {KEY_1}wrong"}),
        await client.get("/providers", headers={"Authorization": f"Bearer {KEY_1}"}),
    ):
        assert KEY_1 not in response.text


# ------------------------------------------------------------------------------------------------ rate limiting


async def test_rate_limit(make_client):
    client, _ = await make_client(make_settings(rate_limit_per_minute=3), providers=[FakeProvider("a", web_results("a"))])
    statuses = []
    for _ in range(4):
        response = await client.get("/search", params={"q": "python"})
        statuses.append(response.status_code)
    assert statuses == [200, 200, 200, 429]
    assert int(response.headers["retry-after"]) >= 1 and response.headers["x-ratelimit-remaining"] == "0"
    assert (await client.get("/health")).status_code == 200  # health checks are never rate limited


async def test_rate_limit_headers_and_per_key_buckets(make_client):
    settings = make_settings(rate_limit_per_minute=2, search_api_key=f"{KEY_1},{KEY_2}")
    client, _ = await make_client(settings, providers=[FakeProvider("a", web_results("a"))])
    one = {"Authorization": f"Bearer {KEY_1}"}
    two = {"Authorization": f"Bearer {KEY_2}"}
    first = await client.get("/search", params={"q": "x"}, headers=one)
    assert first.headers["x-ratelimit-limit"] == "2" and first.headers["x-ratelimit-remaining"] == "1"
    await client.get("/search", params={"q": "x"}, headers=one)
    assert (await client.get("/search", params={"q": "x"}, headers=one)).status_code == 429
    assert (await client.get("/search", params={"q": "x"}, headers=two)).status_code == 200  # another key has its own allowance


async def test_wrong_key_guessing_is_rate_limited_too(make_client):
    client, _ = await make_client(make_settings(rate_limit_per_minute=2, search_api_key=KEY_1), providers=[FakeProvider("a", web_results("a"))])
    codes = [(await client.get("/search", params={"q": "x"}, headers={"Authorization": f"Bearer guess{i}"})).status_code for i in range(3)]
    assert codes == [401, 401, 429]


async def test_forwarded_for_is_ignored_unless_trusted(make_client):
    client, _ = await make_client(make_settings(rate_limit_per_minute=1), providers=[FakeProvider("a", web_results("a"))])
    assert (await client.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
    assert (await client.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "2.2.2.2"})).status_code == 429  # same real client

    trusted, _ = await make_client(make_settings(rate_limit_per_minute=1, trust_proxy_headers=True), providers=[FakeProvider("a", web_results("a"))])
    assert (await trusted.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
    assert (await trusted.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "2.2.2.2"})).status_code == 200  # a different client


async def test_a_client_cannot_spoof_its_address_through_the_trusted_proxy(make_client):
    # the proxy APPENDS the address it saw (5.5.5.5); whatever the client wrote before it must not create a fresh allowance
    client, _ = await make_client(make_settings(rate_limit_per_minute=1, trust_proxy_headers=True), providers=[FakeProvider("a", web_results("a"))])
    assert (await client.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "1.1.1.1, 5.5.5.5"})).status_code == 200
    assert (await client.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "9.9.9.9, 5.5.5.5"})).status_code == 429

    two_hops, _ = await make_client(make_settings(rate_limit_per_minute=1, trust_proxy_headers=True, trusted_proxy_hops=2), providers=[FakeProvider("a", web_results("a"))])
    assert (await two_hops.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "7.7.7.7, 5.5.5.5, 10.0.0.2"})).status_code == 200
    assert (await two_hops.get("/search", params={"q": "x"}, headers={"X-Forwarded-For": "8.8.8.8, 5.5.5.5, 10.0.0.3"})).status_code == 429


# ------------------------------------------------------------------------------------------------ /providers


async def test_providers_endpoint_lists_status_and_never_keys(make_client):
    settings = make_settings(brave_api_key="brave-super-secret-1", google_api_key="AIza-super-secret-2", google_cx="cx-abc", provider_priority="searxng,brave,bing,google,wikipedia")
    client, _ = await make_client(settings, real_providers=True)  # the real provider classes (no request is made)
    response = await client.get("/providers")
    assert response.status_code == 200
    assert "super-secret" not in response.text
    by_name = {p["name"]: p for p in response.json()}
    assert set(by_name) == {"searxng", "brave", "bing", "google", "wikipedia", "newsapi", "google_vision", "ebay"}
    assert by_name["ebay"]["kind"] == "shopping" and by_name["ebay"]["status"] == "not_configured"
    assert by_name["newsapi"]["kind"] == "news" and by_name["newsapi"]["status"] == "not_configured"
    assert by_name["google_vision"]["configured"] is True  # falls back to GOOGLE_API_KEY
    assert by_name["brave"]["status"] == "ok" and by_name["brave"]["configured"] is True and by_name["brave"]["priority"] == 2
    assert by_name["searxng"]["status"] == "not_configured" and by_name["bing"]["enabled"] is False
    assert by_name["wikipedia"]["configured"] is False  # tests turn Wikipedia off by default


async def test_providers_endpoint_shows_cooldowns(make_client):
    quota = FakeProvider("google", error=ProviderQuotaExceeded("Queries per day"))
    client, _ = await make_client(providers=[quota, FakeProvider("wiki", web_results("wiki"))])
    await client.get("/search", params={"q": "python"})
    google = next(p for p in (await client.get("/providers")).json() if p["name"] == "google")
    assert google["status"] == "cooling_down" and google["cooldown_remaining_seconds"] > 3000 and google["last_error"] == "quota_exceeded"


# ------------------------------------------------------------------------------------------------ cross-cutting


async def test_security_headers(make_client):
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    response = await client.get("/search", params={"q": "python"})
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers
    assert "content-security-policy" not in (await client.get("/docs")).headers  # the docs page needs its scripts

    production, _ = await make_client(make_settings(environment="production"), providers=[FakeProvider("a", web_results("a"))])
    assert "max-age" in (await production.get("/health")).headers["strict-transport-security"]


async def test_request_ids(make_client):
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    supplied = await client.get("/search", params={"q": "python"}, headers={"X-Request-ID": "agent-run-42.step-7"})
    assert supplied.headers["x-request-id"] == "agent-run-42.step-7"
    assert supplied.json()["metadata"]["request_id"] == "agent-run-42.step-7"
    generated = await client.get("/search", params={"q": "python"}, headers={"X-Request-ID": "bad id <script>"})
    assert generated.headers["x-request-id"] != "bad id <script>" and len(generated.headers["x-request-id"]) == 32


async def test_an_unexpected_error_is_a_safe_500(make_client):
    class BrokenCache(MemoryCache):
        async def get(self, key):
            raise RuntimeError("internal detail: password=hunter2")

    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))], cache=BrokenCache())
    response = await client.get("/search", params={"q": "python"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error", "request_id": response.headers["x-request-id"]}
    assert "hunter2" not in response.text


async def test_metrics(make_client):
    client, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    await client.get("/search", params={"q": "python"})
    text = (await client.get("/metrics")).text
    for name in ("search_requests_total", "search_request_duration_seconds", "provider_requests_total", "provider_request_duration_seconds"):
        assert name in text, name


async def test_cors_is_off_unless_configured(make_client):
    closed, _ = await make_client(providers=[FakeProvider("a", web_results("a"))])
    assert "access-control-allow-origin" not in (await closed.get("/health", headers={"Origin": "https://evil.example"})).headers
    open_, _ = await make_client(make_settings(cors_origins="https://jonah.example"), providers=[FakeProvider("a", web_results("a"))])
    allowed = await open_.get("/health", headers={"Origin": "https://jonah.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://jonah.example"
    other = await open_.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
