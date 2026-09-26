"""Every provider against a mocked upstream API, and the provider manager's fallback / merge / cool-down behaviour."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.providers.base import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SearchParams,
)
from app.providers.bing import BingProvider
from app.providers.brave import BraveProvider
from app.providers.google import GoogleProvider
from app.providers.health import HealthTracker
from app.providers.manager import ProviderManager, UnknownProviderError
from app.providers.searxng import SearxngProvider
from app.providers.wikipedia import WikipediaProvider
from tests.conftest import FakeProvider, make_settings, result


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(data, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers or {})


def qs(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


# ==================================================================================================== SearXNG

SEARX_OK = {
    "results": [
        {"title": "First <b>hit</b>", "url": "https://example.com/1", "content": "About &amp; more", "publishedDate": "2026-09-20T10:00:00"},
        {"title": "Second", "url": "https://www.other.org/2", "content": None, "publishedDate": None},
        {"title": "No url here"},
        {"url": "https://example.com/no-title"},
        "not a dict",
        {"title": "Bad scheme", "url": "javascript:alert(1)"},
    ]
}


@pytest.mark.anyio
async def test_searxng_success_is_normalised():
    seen = {}

    def handler(request):
        seen.update(qs(request))
        seen["path"] = request.url.path
        seen["accept"] = request.headers["accept"]
        return json_response(SEARX_OK)

    provider = SearxngProvider(make_settings(searxng_url="https://searx.example"), client_for(handler))
    assert provider.is_configured()
    results = await provider.search("latest ai news", 10)
    assert seen["path"] == "/search" and seen["format"] == "json" and seen["q"] == "latest ai news" and seen["accept"] == "application/json"
    assert [r.url for r in results] == ["https://example.com/1", "https://www.other.org/2"]  # the malformed / unsafe items were dropped
    first = results[0]
    assert (first.title, first.snippet, first.rank, first.provider, first.source) == ("First hit", "About & more", 1, "searxng", "example.com")
    assert first.published_at == "2026-09-20T10:00:00Z"
    assert results[1].rank == 2 and results[1].snippet is None and results[1].published_at is None


@pytest.mark.anyio
async def test_searxng_passes_freshness_language_and_domain_filters():
    seen = {}

    def handler(request):
        seen.update(qs(request))
        return json_response({"results": []})

    provider = SearxngProvider(make_settings(searxng_url="https://s.example"), client_for(handler))
    await provider.search("ai", 5, freshness="week", language="pt-BR", domains=("arxiv.org", "openai.com"), exclude_domains=("spam.com",))
    assert seen["time_range"] == "week" and seen["language"] == "pt-BR"
    assert seen["q"] == "ai (site:arxiv.org OR site:openai.com) -site:spam.com"
    await provider.search("ai", 5, freshness="hour")
    assert seen["time_range"] == "day"  # SearXNG has no 'hour'
    seen.clear()
    await provider.search("ai", 5)
    assert "time_range" not in seen and "language" not in seen


@pytest.mark.anyio
async def test_searxng_respects_max_results():
    payload = {"results": [{"title": f"T{i}", "url": f"https://e.com/{i}"} for i in range(30)]}
    provider = SearxngProvider(make_settings(searxng_url="https://s.example"), client_for(lambda r: json_response(payload)))
    assert len(await provider.search("x", 7)) == 7


@pytest.mark.anyio
async def test_searxng_falls_over_to_the_next_instance():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if request.url.host == "broken.example":
            return httpx.Response(503)
        return json_response({"results": [{"title": "Found", "url": "https://e.com/1"}]})

    provider = SearxngProvider(make_settings(searxng_url="https://broken.example, https://good.example/"), client_for(handler))
    assert [r.title for r in await provider.search("x", 5)] == ["Found"]
    assert hits == ["broken.example", "good.example"]
    hits.clear()
    await provider.search("x", 5)  # the failed instance is skipped for a while
    assert hits == ["good.example"]


@pytest.mark.anyio
async def test_searxng_instance_that_refuses_json_is_reported_not_worked_around():
    provider = SearxngProvider(make_settings(searxng_url="https://a.example,https://b.example"), client_for(lambda r: httpx.Response(403, text="forbidden")))
    with pytest.raises(ProviderAuthError):
        await provider.search("x", 5)


@pytest.mark.anyio
async def test_searxng_retries_instances_that_were_all_marked_down():
    calls = []
    provider = SearxngProvider(make_settings(searxng_url="https://a.example"), client_for(lambda r: (calls.append(1), httpx.Response(500))[1]))
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            await provider.search("x", 5)
    assert len(calls) == 2  # with every instance 'down' it still tries (there is nothing better to do)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(200, text="<html>not json</html>"), ProviderBadResponse),
        (httpx.Response(200, json={"no_results_key": []}), ProviderBadResponse),
        (httpx.Response(200, json={"results": "nope"}), ProviderBadResponse),
        (httpx.Response(200, json=["a", "list"]), ProviderBadResponse),
        (httpx.Response(500), ProviderUnavailable),
        (httpx.Response(502), ProviderUnavailable),
        (httpx.Response(429, headers={"retry-after": "30"}), ProviderRateLimited),
        (httpx.Response(404), ProviderBadResponse),
    ],
)
async def test_searxng_failure_modes(response, error):
    provider = SearxngProvider(make_settings(searxng_url="https://s.example"), client_for(lambda r: response))
    with pytest.raises(error):
        await provider.search("x", 5)


@pytest.mark.anyio
async def test_searxng_timeouts_and_connection_errors():
    def slow(request):
        raise httpx.ReadTimeout("slow")

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderTimeout):
        await SearxngProvider(make_settings(searxng_url="https://s.example"), client_for(slow)).search("x", 5)
    with pytest.raises(ProviderUnavailable) as info:
        await SearxngProvider(make_settings(searxng_url="https://s.example"), client_for(down)).search("x", 5)
    assert "refused" not in str(info.value)  # only the exception class is reported


def test_searxng_is_not_configured_without_a_url():
    provider = SearxngProvider(make_settings(), client_for(lambda r: None))
    assert provider.is_configured() is False


# ====================================================================================================== Brave


BRAVE_OK = {
    "web": {
        "results": [
            {"title": "Brave <strong>result</strong>", "url": "https://example.com/a", "description": "desc &quot;quoted&quot;", "page_age": "2026-09-25T08:00:00", "profile": {"name": "Example"}},
            {"title": "Second", "url": "https://x.org/b", "description": "d", "age": "2 days ago", "meta_url": {"hostname": "x.org"}},
        ]
    }
}


@pytest.mark.anyio
async def test_brave_success_headers_and_parameters():
    seen = {}

    def handler(request):
        seen.update(qs(request))
        seen["token"] = request.headers.get("x-subscription-token")
        return json_response(BRAVE_OK)

    provider = BraveProvider(make_settings(brave_api_key="brave-secret-key"), client_for(handler))
    results = await provider.search("ai", 10, freshness="week", language="en-GB", domains=("arxiv.org",))
    assert seen["token"] == "brave-secret-key" and seen["freshness"] == "pw" and seen["search_lang"] == "en" and seen["text_decorations"] == "false"
    assert seen["q"] == "ai site:arxiv.org"
    assert results[0].title == "Brave result" and results[0].snippet == 'desc "quoted"' and results[0].source == "Example"
    assert results[0].published_at == "2026-09-25T08:00:00Z"
    assert results[1].source == "x.org" and results[1].published_at is not None  # "2 days ago" was understood


@pytest.mark.anyio
async def test_brave_freshness_mapping_and_no_freshness_by_default():
    seen = []

    def handler(request):
        seen.append(qs(request).get("freshness"))
        return json_response({"web": {"results": []}})

    provider = BraveProvider(make_settings(brave_api_key="k" * 8), client_for(handler))
    for value in ("hour", "day", "week", "month", "year", "any"):
        await provider.search("x", 3, freshness=value)
    assert seen == ["pd", "pd", "pw", "pm", "py", None]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(401), ProviderAuthError),
        (httpx.Response(403), ProviderAuthError),
        (httpx.Response(402), ProviderQuotaExceeded),
        (httpx.Response(429, headers={"retry-after": "12"}), ProviderRateLimited),
        (httpx.Response(503), ProviderUnavailable),
        (httpx.Response(200, text="garbage"), ProviderBadResponse),
        (httpx.Response(200, json={"web": []}), ProviderBadResponse),  # wrong shape
    ],
)
async def test_brave_failure_modes(response, error):
    provider = BraveProvider(make_settings(brave_api_key="brave-secret-key"), client_for(lambda r: response))
    with pytest.raises(error) as info:
        await provider.search("x", 5)
    assert "brave-secret-key" not in str(info.value)


@pytest.mark.anyio
async def test_a_missing_result_list_is_an_empty_search_but_a_wrong_type_is_malformed():
    settings = make_settings(brave_api_key="k" * 8)
    assert await BraveProvider(settings, client_for(lambda r: json_response({"type": "search"}))).search("x", 5) == []  # nothing matched
    assert await BraveProvider(settings, client_for(lambda r: json_response({"web": {}}))).search("x", 5) == []
    skipped = {"web": {"results": ["junk", 3, {"title": "Kept", "url": "https://k.com/"}]}}
    assert [r.title for r in await BraveProvider(settings, client_for(lambda r: json_response(skipped))).search("x", 5)] == ["Kept"]
    for bad in ({"web": {"results": {"a": 1}}}, ["not", "an", "object"], {"web": "text"}):
        with pytest.raises(ProviderBadResponse):
            await BraveProvider(settings, client_for(lambda r, b=bad: json_response(b))).search("x", 5)


@pytest.mark.anyio
async def test_brave_reports_retry_after():
    provider = BraveProvider(make_settings(brave_api_key="k" * 8), client_for(lambda r: httpx.Response(429, headers={"retry-after": "45"})))
    with pytest.raises(ProviderRateLimited) as info:
        await provider.search("x", 5)
    assert info.value.retry_after == 45.0


def test_brave_needs_a_key():
    assert BraveProvider(make_settings(), client_for(lambda r: None)).is_configured() is False
    assert BraveProvider(make_settings(brave_api_key="  "), client_for(lambda r: None)).is_configured() is False
    assert BraveProvider(make_settings(brave_api_key="abc"), client_for(lambda r: None)).is_configured() is True


# ====================================================================================================== Bing


@pytest.mark.anyio
async def test_bing_success_and_parameters():
    seen = {}

    def handler(request):
        seen.update(qs(request))
        seen["key"] = request.headers.get("ocp-apim-subscription-key")
        seen["host"] = request.url.host
        return json_response({"webPages": {"value": [{"name": "Bing result", "url": "https://example.com/b", "snippet": "s", "datePublished": "2026-09-01T00:00:00.0000000Z"}]}})

    provider = BingProvider(make_settings(bing_api_key="bing-key"), client_for(handler))
    results = await provider.search("ai", 5, freshness="month", language="fr")
    assert seen["key"] == "bing-key" and seen["freshness"] == "Month" and seen["setLang"] == "fr" and seen["host"] == "api.bing.microsoft.com"
    assert results[0].title == "Bing result" and results[0].published_at == "2026-09-01T00:00:00Z"
    seen.clear()
    await provider.search("ai", 5, freshness="year")
    assert "freshness" not in seen  # Bing has no year window: ignored, not an error


@pytest.mark.anyio
async def test_bing_endpoint_can_be_overridden_and_failures_are_mapped():
    hosts = []
    provider = BingProvider(make_settings(bing_api_key="k" * 8, bing_endpoint="https://custom.example/v7/search"), client_for(lambda r: (hosts.append(r.url.host), httpx.Response(401))[1]))
    with pytest.raises(ProviderAuthError):
        await provider.search("x", 5)
    assert hosts == ["custom.example"]
    with pytest.raises(ProviderBadResponse):
        await BingProvider(make_settings(bing_api_key="k" * 8), client_for(lambda r: json_response({"webPages": "wrong"}))).search("x", 5)


# ==================================================================================================== Google


GOOGLE_OK = {
    "items": [
        {"title": "Google result", "link": "https://example.com/g", "snippet": "s", "displayLink": "example.com", "pagemap": {"metatags": [{"article:published_time": "2026-08-30T12:00:00Z"}]}},
        {"title": "No pagemap", "link": "https://x.org/", "snippet": "d"},
    ]
}


@pytest.mark.anyio
async def test_google_success_and_parameters():
    seen = {}

    def handler(request):
        seen.update(qs(request))
        return json_response(GOOGLE_OK)

    provider = GoogleProvider(make_settings(google_api_key="AIza-secret", google_cx="cx123"), client_for(handler))
    results = await provider.search("ai", 25, freshness="day", language="pt-BR")
    assert seen["cx"] == "cx123" and seen["dateRestrict"] == "d1" and seen["lr"] == "lang_pt" and seen["num"] == "10"  # Google caps a request at 10
    assert results[0].published_at == "2026-08-30T12:00:00Z" and results[0].source == "example.com"
    assert results[1].published_at is None and results[1].source == "x.org"


@pytest.mark.anyio
async def test_google_daily_quota_is_reported_as_quota_exceeded():
    body = {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per day' of service 'customsearch.googleapis.com'"}}
    provider = GoogleProvider(make_settings(google_api_key="AIza-secret", google_cx="cx"), client_for(lambda r: json_response(body, 429)))
    with pytest.raises(ProviderQuotaExceeded) as info:
        await provider.search("x", 5)
    assert "Queries per day" in str(info.value)


@pytest.mark.anyio
async def test_google_per_minute_limits_are_only_a_short_rate_limit():
    body = {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'"}}
    provider = GoogleProvider(make_settings(google_api_key="AIza-secret", google_cx="cx"), client_for(lambda r: json_response(body, 429, {"retry-after": "20"})))
    with pytest.raises(ProviderRateLimited) as info:
        await provider.search("x", 5)
    assert info.value.retry_after == 20.0


@pytest.mark.anyio
async def test_google_bad_key_and_disabled_api_are_auth_failures_and_never_echo_the_key():
    leaky = "API key not valid. url=https://www.googleapis.com/customsearch/v1?key=AIza-secret&cx=cx"
    for status, message in ((400, leaky), (403, "Custom Search API has not been used in project 1 before or it is disabled.")):
        provider = GoogleProvider(make_settings(google_api_key="AIza-secret", google_cx="cx"), client_for(lambda r, s=status, m=message: json_response({"error": {"message": m}}, s)))
        with pytest.raises(ProviderAuthError) as info:
            await provider.search("x", 5)
        assert "AIza-secret" not in str(info.value)


@pytest.mark.anyio
async def test_google_other_failures():
    settings = make_settings(google_api_key="AIza-secret", google_cx="cx")
    with pytest.raises(ProviderUnavailable):
        await GoogleProvider(settings, client_for(lambda r: httpx.Response(503))).search("x", 5)
    with pytest.raises(ProviderBadResponse):
        await GoogleProvider(settings, client_for(lambda r: httpx.Response(200, text="<html>"))).search("x", 5)


def test_google_needs_both_a_key_and_an_engine_id():
    assert GoogleProvider(make_settings(google_api_key="k" * 8), client_for(lambda r: None)).is_configured() is False
    assert GoogleProvider(make_settings(google_cx="cx"), client_for(lambda r: None)).is_configured() is False
    assert GoogleProvider(make_settings(google_api_key="k" * 8, google_cx="cx"), client_for(lambda r: None)).is_configured() is True


# ================================================================================================= Wikipedia


@pytest.mark.anyio
async def test_wikipedia_success():
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen.update(qs(request))
        return json_response({"query": {"search": [{"title": "Python (programming language)", "snippet": 'A <span class="searchmatch">Python</span> &amp; more'}, {"title": ""}, {"nope": 1}]}})

    provider = WikipediaProvider(make_settings(enable_wikipedia=True), client_for(handler))
    assert provider.is_configured()
    results = await provider.search("python", 5)
    assert seen["host"] == "en.wikipedia.org" and seen["srsearch"] == "python" and seen["action"] == "query"
    assert len(results) == 1
    assert results[0].url == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert results[0].snippet == "A Python & more"


@pytest.mark.anyio
async def test_wikipedia_uses_the_requested_language_and_rejects_nonsense():
    hosts = []
    provider = WikipediaProvider(make_settings(enable_wikipedia=True), client_for(lambda r: (hosts.append(r.url.host), json_response({"query": {"search": []}}))[1]))
    await provider.search("x", 5, language="pt-BR")
    await provider.search("x", 5, language="../evil")
    assert hosts == ["pt.wikipedia.org", "en.wikipedia.org"]


def test_wikipedia_can_be_disabled():
    assert WikipediaProvider(make_settings(enable_wikipedia=False), client_for(lambda r: None)).is_configured() is False


# ===================================================================================== provider descriptions


def test_describe_never_contains_a_key():
    provider = BraveProvider(make_settings(brave_api_key="super-secret-brave-key"), client_for(lambda r: None))
    assert "super-secret" not in repr(provider.describe())
    assert provider.describe()["configured"] is True


# ============================================================================================= the manager


def manager_for(providers, **settings):
    return ProviderManager(providers, make_settings(**settings))


def params(**kw) -> SearchParams:
    return SearchParams(**{"query": "test", "max_results": 10, **kw})


@pytest.mark.anyio
async def test_fast_mode_uses_the_first_healthy_provider_in_priority_order():
    a = FakeProvider("a", [result("A", "https://a.com/1", provider="a")])
    b = FakeProvider("b", [result("B", "https://b.com/1", provider="b")])
    outcome = await manager_for([b, a], provider_priority="a,b").search(params())
    assert outcome.used == ["a"] and [r.title for r in outcome.results] == ["A"]
    assert len(a.calls) == 1 and len(b.calls) == 0  # the second provider was never called


@pytest.mark.anyio
async def test_a_failing_provider_is_followed_by_the_next_and_the_error_is_reported():
    a = FakeProvider("a", error=ProviderUnavailable("HTTP 503"))
    b = FakeProvider("b", [result("B", "https://b.com/1", provider="b")])
    c = FakeProvider("c", [result("C", "https://c.com/1", provider="c")])
    outcome = await manager_for([a, b, c], provider_priority="a,b,c").search(params())
    assert outcome.used == ["b"]
    assert [(e.provider, e.error, e.detail) for e in outcome.errors] == [("a", "unavailable", "HTTP 503")]
    assert len(c.calls) == 0


@pytest.mark.anyio
async def test_the_full_fallback_chain_searxng_brave_bing_google():
    chain = [
        FakeProvider("searxng", error=ProviderTimeout("timed out")),
        FakeProvider("brave", configured=False),
        FakeProvider("bing", error=ProviderBadResponse("bad json")),
        FakeProvider("google", [result("G", "https://g.com/1", provider="google")]),
    ]
    outcome = await manager_for(chain, provider_priority="searxng,brave,bing,google").search(params())
    assert outcome.used == ["google"]
    assert [e.provider for e in outcome.errors] == ["searxng", "bing"]  # brave was never a candidate: it is not configured


@pytest.mark.anyio
async def test_an_empty_answer_moves_on_to_the_next_provider_without_counting_as_a_failure():
    a = FakeProvider("a", [])
    b = FakeProvider("b", [result("B", "https://b.com/1", provider="b")])
    manager = manager_for([a, b], provider_priority="a,b")
    outcome = await manager.search(params())
    assert outcome.used == ["b"] and outcome.errors == [] and outcome.answered == ["a", "b"]
    assert manager.health.get("a").failures == 0


@pytest.mark.anyio
async def test_when_every_provider_fails_nothing_is_raised():
    providers = [FakeProvider("a", error=ProviderTimeout("t")), FakeProvider("b", error=ProviderQuotaExceeded("q"))]
    outcome = await manager_for(providers, provider_priority="a,b").search(params())
    assert outcome.results == [] and outcome.used == [] and outcome.answered == []
    assert [e.error for e in outcome.errors] == ["timeout", "quota_exceeded"]


@pytest.mark.anyio
async def test_a_provider_that_crashes_with_an_unexpected_exception_does_not_take_the_request_down():
    class Buggy(FakeProvider):
        async def _search(self, params):
            raise RuntimeError("bug with secret details")

    outcome = await manager_for([Buggy("buggy"), FakeProvider("ok", [result("OK", "https://o.com/1", provider="ok")])], provider_priority="buggy,ok").search(params())
    assert outcome.used == ["ok"]
    assert outcome.errors[0].provider == "buggy" and "secret" not in (outcome.errors[0].detail or "")


@pytest.mark.anyio
async def test_a_slow_provider_times_out_and_the_next_one_answers():
    # each provider's time budget comes from ITS settings (REQUEST_TIMEOUT_SECONDS) plus one second of grace
    slow = FakeProvider("slow", [result("S", "https://s.com/1", provider="slow")], delay=10.0, settings=make_settings(request_timeout_seconds=0.2))
    fast = FakeProvider("fast", [result("F", "https://f.com/1", provider="fast")])
    started = asyncio.get_event_loop().time()
    outcome = await manager_for([slow, fast], provider_priority="slow,fast").search(params())
    assert outcome.used == ["fast"] and outcome.errors[0].error == "timeout"
    assert asyncio.get_event_loop().time() - started < 3.0


@pytest.mark.anyio
async def test_deep_mode_queries_providers_in_parallel_and_merges_everything():
    a = FakeProvider("a", [result("Shared", "https://e.com/x?utm_source=z", 1, "a"), result("Only A", "https://a.com/1", 2, "a")], delay=0.15)
    b = FakeProvider("b", [result("Shared", "https://www.e.com/x", 1, "b"), result("Only B", "https://b.com/1", 2, "b")], delay=0.15)
    started = asyncio.get_event_loop().time()
    outcome = await manager_for([a, b], provider_priority="a,b").search(params(), mode="deep")
    elapsed = asyncio.get_event_loop().time() - started
    assert sorted(outcome.used) == ["a", "b"] and len(outcome.results) == 4
    assert elapsed < 0.28  # ~0.15s in parallel, not 0.30 one after the other


@pytest.mark.anyio
async def test_deep_mode_survives_one_provider_failing():
    a = FakeProvider("a", [result("A", "https://a.com/1", provider="a")])
    b = FakeProvider("b", error=ProviderUnavailable("down"))
    outcome = await manager_for([a, b], provider_priority="a,b").search(params(), mode="deep")
    assert outcome.used == ["a"] and [e.provider for e in outcome.errors] == ["b"]


@pytest.mark.anyio
async def test_deep_mode_is_limited_to_the_configured_number_of_providers():
    providers = [FakeProvider(n, [result(n, f"https://{n}.com/", provider=n)]) for n in "abcde"]
    outcome = await manager_for(providers, provider_priority="a,b,c,d,e", deep_mode_max_providers=2).search(params(), mode="deep")
    assert sorted(outcome.used) == ["a", "b"]


@pytest.mark.anyio
async def test_a_specific_provider_can_be_requested_and_bad_names_are_rejected():
    a = FakeProvider("a", [result("A", "https://a.com/1", provider="a")])
    b = FakeProvider("b", [result("B", "https://b.com/1", provider="b")])
    manager = manager_for([a, b], provider_priority="a,b")
    assert (await manager.search(params(), provider="b")).used == ["b"]
    with pytest.raises(UnknownProviderError):
        await manager.search(params(), provider="nope")
    with pytest.raises(UnknownProviderError):
        await manager_for([FakeProvider("off", configured=False)]).search(params(), provider="off")


@pytest.mark.anyio
async def test_unconfigured_providers_are_never_used_and_no_providers_is_reported_clearly():
    outcome = await manager_for([FakeProvider("a", configured=False)]).search(params())
    assert outcome.candidates == 0 and outcome.results == [] and outcome.errors == []
    assert (await manager_for([]).search(params())).candidates == 0


def test_priority_comes_from_configuration_and_unknown_names_are_ignored():
    manager = manager_for([FakeProvider("a"), FakeProvider("b"), FakeProvider("c")], provider_priority="c, ghost ,a")
    assert [p.name for p in manager.all_providers] == ["c", "a", "b"]  # configured order first, the rest afterwards
    assert manager.priority_of("c") == 1 and manager.priority_of("zzz") is None


@pytest.mark.anyio
async def test_domain_filters_are_only_sent_to_providers_that_understand_site_operators():
    class NoOperators(FakeProvider):
        supports_site_operators = False

    plain = NoOperators("plain", [result("P", "https://p.com/1", provider="plain")])
    operators = FakeProvider("ops", [result("O", "https://o.com/1", provider="ops")])
    await manager_for([plain, operators], provider_priority="plain,ops").search(params(domains=("arxiv.org",), exclude_domains=("x.com",)), mode="deep")
    assert plain.calls[0].domains == () and operators.calls[0].domains == ("arxiv.org",)


@pytest.mark.anyio
async def test_extra_results_are_requested_when_filters_will_remove_some():
    provider = FakeProvider("a", [result("A", "https://a.com/1", provider="a")])
    await manager_for([provider]).search(params(max_results=5))
    await manager_for([provider]).search(params(max_results=5, domains=("a.com",)))
    assert [c.max_results for c in provider.calls] == [5, 10]


# ---------------------------------------------------------------------------------------- cool-downs (health)


@pytest.mark.anyio
async def test_a_provider_out_of_quota_is_skipped_and_the_skip_is_reported():
    quota = FakeProvider("google", error=ProviderQuotaExceeded("Queries per day"))
    backup = FakeProvider("wiki", [result("W", "https://w.org/1", provider="wiki")])
    manager = manager_for([quota, backup], provider_priority="google,wiki")
    first = await manager.search(params())
    assert first.used == ["wiki"] and len(quota.calls) == 1
    second = await manager.search(params())
    assert len(quota.calls) == 1  # NOT asked again: an exhausted daily quota does not come back in seconds
    assert second.used == ["wiki"]
    assert (second.errors[0].provider, second.errors[0].error) == ("google", "cooling_down")
    assert "quota_exceeded" in (second.errors[0].detail or "")


@pytest.mark.anyio
async def test_an_explicitly_requested_provider_is_tried_even_while_cooling_down():
    quota = FakeProvider("google", error=ProviderQuotaExceeded("q"))
    manager = manager_for([quota])
    await manager.search(params())
    await manager.search(params(), provider="google")
    assert len(quota.calls) == 2


def test_cooldown_rules():
    now = [1000.0]
    health = HealthTracker(clock=lambda: now[0])
    health.record_failure("g", ProviderQuotaExceeded("q"), 5)
    assert health.cooldown_remaining("g") == 3600
    health.record_failure("k", ProviderAuthError("bad key"), 5)
    assert health.cooldown_remaining("k") == 600
    health.record_failure("r", ProviderRateLimited("slow down", retry_after=20), 5)
    assert health.cooldown_remaining("r") == 20
    health.record_failure("r2", ProviderRateLimited("slow down"), 5)
    assert health.cooldown_remaining("r2") == 60


def test_ordinary_failures_only_cool_down_after_three_in_a_row_and_back_off():
    now = [0.0]
    health = HealthTracker(clock=lambda: now[0])
    for _ in range(2):
        health.record_failure("p", ProviderUnavailable("x"), 1)
    assert health.cooldown_remaining("p") == 0  # two blips are not an outage
    health.record_failure("p", ProviderUnavailable("x"), 1)
    assert health.cooldown_remaining("p") == 30
    health.record_failure("p", ProviderUnavailable("x"), 1)
    assert health.cooldown_remaining("p") == 60
    for _ in range(10):
        health.record_failure("p", ProviderUnavailable("x"), 1)
    assert health.cooldown_remaining("p") == 300  # capped
    now[0] = 400.0
    assert health.cooldown_remaining("p") == 0  # and it expires on its own


def test_one_success_clears_the_failure_count():
    health = HealthTracker()
    for _ in range(2):
        health.record_failure("p", ProviderUnavailable("x"), 1)
    health.record_success("p", 12.0)
    health.record_failure("p", ProviderUnavailable("x"), 1)
    health.record_failure("p", ProviderUnavailable("x"), 1)
    assert health.cooldown_remaining("p") == 0 and health.get("p").consecutive_failures == 2


def test_error_codes_are_stable():
    assert {c.code for c in (ProviderTimeout, ProviderUnavailable, ProviderQuotaExceeded, ProviderRateLimited, ProviderAuthError, ProviderBadResponse)} == {
        "timeout", "unavailable", "quota_exceeded", "rate_limited", "auth_failed", "malformed_response",
    }  # fmt: skip
    assert issubclass(ProviderTimeout, ProviderError)
