"""Configuration, logging redaction, caches, rate limiters and small helpers."""

from __future__ import annotations

import io
import json
import logging

import pytest
from pydantic import ValidationError

from app.cache import build_cache
from app.cache.memory import MemoryCache
from app.cache.redis import RedisCache
from app.config import Settings
from app.logging_config import JsonFormatter, log_event, request_id_var
from app.ratelimit import MemoryRateLimiter, NullRateLimiter, RedisRateLimiter, build_rate_limiter
from app.util import domain_matches, hostname_of, parse_datetime, strip_tags
from tests.conftest import make_settings

# ================================================================================================= configuration


def test_settings_come_from_environment_variables(monkeypatch):
    monkeypatch.setenv("MAX_RESULTS", "7")
    monkeypatch.setenv("PROVIDER_PRIORITY", " Brave , searxng,,GOOGLE ")
    monkeypatch.setenv("SEARCH_API_KEY", "key-one, key-two ,")
    monkeypatch.setenv("SEARXNG_URL", "https://a.example/, https://b.example")
    monkeypatch.setenv("RESPECT_ROBOTS", "false")
    settings = Settings(_env_file=None)
    assert settings.max_results == 7 and settings.respect_robots is False
    assert settings.priority_list == ["brave", "searxng", "google"]
    assert settings.api_keys == ["key-one", "key-two"]
    assert settings.searxng_urls == ["https://a.example", "https://b.example"]


def test_defaults_are_safe():
    settings = Settings(_env_file=None)
    assert settings.api_keys == [] and settings.respect_robots is True and settings.robots_fail_open is False
    assert settings.allowed_port_set == frozenset({80, 443}) and settings.trust_proxy_headers is False
    assert settings.max_page_size_bytes == 5 * 1024 * 1024


def test_ports_fall_back_to_web_ports_when_nonsense():
    assert make_settings(allowed_ports="80, 443, 8080").allowed_port_set == frozenset({80, 443, 8080})
    assert make_settings(allowed_ports="abc,,").allowed_port_set == frozenset({80, 443})


@pytest.mark.parametrize(
    "field, value",
    [
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", -5),
        ("max_results", 0),
        ("max_results", 1000),
        ("max_redirects", -1),
        ("max_concurrent_fetches", 0),
        ("max_page_size_mb", 0),
        ("rate_limit_per_minute", -1),
        ("deep_mode_max_providers", 0),
        ("cache_ttl_seconds", -1),
    ],
)
def test_out_of_range_settings_fail_at_startup(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_secrets_to_redact_lists_every_configured_secret():
    settings = make_settings(brave_api_key="brave-123456", google_api_key="google-123456", search_api_key="api-one-123,api-two-456", bing_api_key="abc")
    assert set(settings.secrets_to_redact()) == {"brave-123456", "google-123456", "api-one-123", "api-two-456"}  # too-short values are not masked


def test_secret_settings_are_not_shown_in_their_repr():
    settings = make_settings(brave_api_key="brave-very-secret")
    assert "brave-very-secret" not in repr(settings) and "brave-very-secret" not in str(settings.model_dump())


# ======================================================================================================= logging


def capture(secrets=None):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(secrets))
    logger = logging.getLogger(f"test.capture.{id(stream)}")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_log_lines_are_json_with_the_request_id():
    logger, stream = capture()
    token = request_id_var.set("req-12345678")
    try:
        log_event(logger, "search", provider="searxng", results=3, duration_ms=12)
    finally:
        request_id_var.reset(token)
    line = json.loads(stream.getvalue())
    assert line["event"] == "search" and line["provider"] == "searxng" and line["results"] == 3 and line["request_id"] == "req-12345678"
    assert line["level"] == "INFO" and line["ts"].endswith("Z")


def test_sensitive_fields_are_dropped_and_secret_values_masked():
    logger, stream = capture(secrets=["AIza-live-key-9876", 'we"ird\\key-123'])
    log_event(logger, "provider_error", authorization="Bearer abc", api_key="xyz", token="t", detail="upstream said key=AIza-live-key-9876 is bad")
    logger.warning("formatted message with %s", "AIza-live-key-9876")
    log_event(logger, "odd", detail='the key we"ird\\key-123 leaked')
    lines = [json.loads(l) for l in stream.getvalue().splitlines()]
    assert "authorization" not in lines[0] and "api_key" not in lines[0] and "token" not in lines[0]
    assert "AIza-live-key-9876" not in stream.getvalue() and "[redacted]" in lines[0]["detail"] and "[redacted]" in lines[1]["message"]
    assert "ird" not in lines[2]["detail"]  # a secret containing JSON-escaped characters is masked too


# ======================================================================================================== caches


@pytest.mark.anyio
async def test_memory_cache_ttl():
    now = [100.0]
    cache = MemoryCache(10, clock=lambda: now[0])
    await cache.set("k", {"a": 1}, 60)
    assert await cache.get("k") == {"a": 1}
    now[0] = 159.0
    assert await cache.get("k") == {"a": 1}
    now[0] = 160.0
    assert await cache.get("k") is None and len(cache) == 0
    await cache.set("zero", 1, 0)  # ttl 0 = do not cache
    assert await cache.get("zero") is None


@pytest.mark.anyio
async def test_memory_cache_lru_eviction_and_copies():
    cache = MemoryCache(2)
    await cache.set("a", [1], 60)
    await cache.set("b", [2], 60)
    await cache.get("a")  # a is now the most recently used
    await cache.set("c", [3], 60)
    assert await cache.get("b") is None and await cache.get("a") == [1] and await cache.get("c") == [3]
    value = await cache.get("a")
    value.append("mutated")
    assert await cache.get("a") == [1]  # callers get a copy


@pytest.mark.anyio
async def test_memory_cache_ignores_unserialisable_values():
    cache = MemoryCache(2)
    circular: list = []
    circular.append(circular)
    await cache.set("x", circular, 60)
    assert await cache.get("x") is None


class BrokenRedis:
    def __init__(self):
        self.closed = False

    async def get(self, key):
        raise ConnectionError("redis down")

    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def incr(self, key):
        raise ConnectionError("redis down")

    async def aclose(self):
        self.closed = True


class DictRedis:
    """Just enough of redis.asyncio for the cache and the rate limiter."""

    def __init__(self):
        self.data: dict = {}
        self.expiry: dict = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.expiry[key] = ex

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key, seconds):
        self.expiry[key] = seconds

    async def aclose(self):
        return None


@pytest.mark.anyio
async def test_redis_cache_round_trip_and_ttl():
    fake = DictRedis()
    cache = RedisCache(fake)
    await cache.set("k", {"x": [1, 2]}, 30)
    assert await cache.get("k") == {"x": [1, 2]}
    assert fake.expiry["jonah-search:cache:k"] == 30


@pytest.mark.anyio
async def test_a_redis_outage_is_a_cache_miss_not_an_error():
    cache = RedisCache(BrokenRedis())
    await cache.set("k", 1, 30)  # does not raise
    assert await cache.get("k") is None


@pytest.mark.anyio
async def test_build_cache_falls_back_to_memory(monkeypatch):
    async def refuse(cls, url):
        raise ConnectionError("no redis here")

    monkeypatch.setattr(RedisCache, "connect", classmethod(refuse))
    assert (await build_cache(make_settings(redis_url="redis://cache.example:6379/0"))).name == "memory"
    assert (await build_cache(make_settings())).name == "memory"


# ================================================================================================ rate limiters


@pytest.mark.anyio
async def test_memory_rate_limiter_sliding_window():
    now = [0.0]
    limiter = MemoryRateLimiter(2, clock=lambda: now[0])
    assert (await limiter.hit("a")).allowed and (await limiter.hit("a")).remaining == 0
    blocked = await limiter.hit("a")
    assert not blocked.allowed and 1 <= blocked.retry_after <= 61
    assert (await limiter.hit("b")).allowed  # other clients are independent
    now[0] = 61.0
    assert (await limiter.hit("a")).allowed


@pytest.mark.anyio
async def test_memory_rate_limiter_cleanup_never_loses_a_hit():
    now = [0.0]
    limiter = MemoryRateLimiter(1, clock=lambda: now[0])
    for i in range(498):
        await limiter.hit(f"old-{i}")
    now[0] = 100.0
    await limiter.hit("filler")  # call 499
    assert (await limiter.hit("new")).allowed  # call 500 triggers the cleanup
    assert not (await limiter.hit("new")).allowed  # ...and the first hit was still counted
    assert "old-0" not in limiter._hits


@pytest.mark.anyio
async def test_redis_rate_limiter_counts_per_window_and_fails_open():
    limiter = RedisRateLimiter(DictRedis(), 2, wall=lambda: 120.0)
    assert [(await limiter.hit("c")).allowed for _ in range(3)] == [True, True, False]
    broken = RedisRateLimiter(BrokenRedis(), 2)
    assert all([(await broken.hit("c")).allowed for _ in range(5)])  # an outage allows requests rather than blocking everyone


@pytest.mark.anyio
async def test_rate_limit_zero_disables_it():
    assert isinstance(await build_rate_limiter(make_settings(rate_limit_per_minute=0)), NullRateLimiter)
    assert isinstance(await build_rate_limiter(make_settings(rate_limit_per_minute=10)), MemoryRateLimiter)


# ======================================================================================================= helpers


NOW = __import__("datetime").datetime(2026, 9, 26, 12, 0, tzinfo=__import__("datetime").timezone.utc)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z"),
        ("2026-09-20T10:00:00+02:00", "2026-09-20T08:00:00Z"),
        ("2026-09-20T10:00:00", "2026-09-20T10:00:00Z"),  # no zone: taken as UTC
        ("2026-09-20", "2026-09-20T00:00:00Z"),
        ("Sat, 20 Sep 2026 10:00:00 GMT", "2026-09-20T10:00:00Z"),
        ("2 days ago", "2026-09-24T12:00:00Z"),
        ("1 hour ago", "2026-09-26T11:00:00Z"),
        (1790000000, "2026-09-21T14:13:20Z"),
        (1790000000000, "2026-09-21T14:13:20Z"),  # milliseconds
        ("yesterday-ish", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_datetime(value, expected):
    assert parse_datetime(value, now=NOW) == expected


def test_hostnames_and_domain_matching():
    assert hostname_of("https://WWW.Example.COM:8443/path") == "example.com"
    assert hostname_of("not a url") == "" and hostname_of("http://[::1") == ""
    assert domain_matches("news.example.com", "example.com") and domain_matches("example.com", "www.example.com")
    assert not domain_matches("badexample.com", "example.com") and not domain_matches("", "example.com")


def test_strip_tags():
    assert strip_tags("<b>Python</b> &amp; <i>more</i>\n  text") == "Python & more text"
    assert strip_tags(None) == "" and strip_tags("") == ""
