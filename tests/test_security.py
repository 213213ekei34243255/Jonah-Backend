"""SSRF protection and safe fetching. Nothing here touches the network: DNS is faked and HTTP is served by httpx.MockTransport."""

from __future__ import annotations

import gzip
import ipaddress

import httpx
import pytest

from app.scraper.fetcher import FetchError, SafeHttpClient
from app.scraper.security import UnsafeURLError, is_blocked_ip, resolve_public_ips, validate_url
from tests.conftest import make_settings

PUBLIC_IP = "93.184.216.34"


# ------------------------------------------------------------------------------------------------ URL validation


@pytest.mark.parametrize(
    "url, code",
    [
        ("file:///etc/passwd", "unsupported_scheme"),
        ("ftp://example.com/file", "unsupported_scheme"),
        ("gopher://example.com/", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("data:text/html,<script>", "unsupported_scheme"),
        ("//example.com/x", "unsupported_scheme"),
        ("example.com/x", "unsupported_scheme"),
        ("http://", "invalid_url"),
        ("", "invalid_url"),
        ("   ", "invalid_url"),
        ("http://user:pass@example.com/", "invalid_url"),
        ("http://user@example.com/", "invalid_url"),
        ("http://localhost/", "blocked_host"),
        ("http://LOCALHOST:80/", "blocked_host"),
        ("http://localhost./", "blocked_host"),
        ("http://foo.localhost/", "blocked_host"),
        ("http://metadata.google.internal/computeMetadata/v1/", "blocked_host"),
        ("http://service.internal/", "blocked_host"),
        ("http://printer.local/", "blocked_host"),
        ("http://router.lan/", "blocked_host"),
        ("http://example.com:22/", "blocked_port"),
        ("http://example.com:6379/", "blocked_port"),
        ("http://example.com:8080/", "blocked_port"),  # only 80/443 by default
        ("http://example.com:99999/", "invalid_url"),
        ("https://example.com/" + "a" * 3000, "invalid_url"),
    ],
)
def test_unsafe_urls_are_rejected(url, code):
    with pytest.raises(UnsafeURLError) as info:
        validate_url(url)
    assert info.value.code == code


def test_a_normal_url_is_accepted_and_parsed():
    parts = validate_url("https://Example.com/a/b?x=1&y=2")
    assert (parts.scheme, parts.host, parts.port, parts.path_query, parts.host_header) == ("https", "example.com", 443, "/a/b?x=1&y=2", "example.com")
    assert validate_url("http://example.com").path_query == "/"
    assert validate_url("http://example.com:80/x").host_header == "example.com"
    assert validate_url("https://example.com:443/x").port == 443


def test_allowed_ports_can_be_extended():
    assert validate_url("http://example.com:8080/", frozenset({80, 443, 8080})).port == 8080
    assert validate_url("http://example.com:8080/", frozenset({80, 443, 8080})).host_header == "example.com:8080"


def test_unicode_hosts_are_converted_to_ascii():
    assert validate_url("https://bücher.example/").host == "xn--bcher-kva.example"


# ----------------------------------------------------------------------------------------------- blocked addresses


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.0.1", "192.168.255.255",
        "169.254.169.254", "169.254.0.1", "0.0.0.0", "100.64.0.1", "100.100.100.200", "224.0.0.1", "240.0.0.1", "255.255.255.255",
        "192.0.2.1", "198.51.100.7", "203.0.113.9",
        "::1", "::", "fc00::1", "fd00:ec2::254", "fe80::1", "ff02::1", "2001:db8::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",  # IPv4 hidden inside IPv6
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
    ],
)  # fmt: skip
def test_non_public_addresses_are_blocked(address):
    assert is_blocked_ip(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", PUBLIC_IP, "151.101.1.69", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e"])
def test_public_addresses_are_allowed(address):
    assert is_blocked_ip(ipaddress.ip_address(address)) is False


# ------------------------------------------------------------------------------------------------------ resolving


def resolver_for(mapping):
    calls = []

    async def resolve(host, port):
        calls.append(host)
        answer = mapping[host]
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    resolve.calls = calls
    return resolve


@pytest.mark.anyio
async def test_a_host_resolving_to_a_public_address_is_allowed():
    ips = await resolve_public_ips("example.com", 443, resolver_for({"example.com": [PUBLIC_IP]}))
    assert [str(i) for i in ips] == [PUBLIC_IP]


@pytest.mark.anyio
@pytest.mark.parametrize("private", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254", "::1", "fd00::1"])
async def test_a_host_resolving_to_a_private_address_is_blocked(private):
    with pytest.raises(UnsafeURLError) as info:
        await resolve_public_ips("evil.example", 443, resolver_for({"evil.example": [private]}))
    assert info.value.code == "blocked_address"


@pytest.mark.anyio
async def test_one_private_address_among_public_ones_blocks_the_whole_lookup():
    """A DNS answer that mixes a public and a private address must not be trusted (rebinding trick)."""
    with pytest.raises(UnsafeURLError) as info:
        await resolve_public_ips("mixed.example", 443, resolver_for({"mixed.example": [PUBLIC_IP, "10.0.0.5"]}))
    assert info.value.code == "blocked_address"


@pytest.mark.anyio
async def test_literal_ip_hosts_are_checked_without_any_dns_lookup():
    resolver = resolver_for({})
    for literal in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:7f00:1"):
        with pytest.raises(UnsafeURLError):
            await resolve_public_ips(literal, 80, resolver)
    assert resolver.calls == []
    assert [str(i) for i in await resolve_public_ips("8.8.8.8", 80, resolver)] == ["8.8.8.8"]


@pytest.mark.anyio
async def test_odd_ip_spellings_are_caught_because_the_RESOLVED_address_is_what_counts():
    """http://2130706433/ and http://0x7f.1/ are 127.0.0.1 in disguise: the resolver turns them into it, and that is checked."""
    for spelling in ("2130706433", "0x7f.1", "017700000001", "127.1"):
        with pytest.raises(UnsafeURLError):
            await resolve_public_ips(spelling, 80, resolver_for({spelling: ["127.0.0.1"]}))


@pytest.mark.anyio
async def test_dns_failures_are_reported_not_raised_raw():
    with pytest.raises(UnsafeURLError) as info:
        await resolve_public_ips("nope.example", 80, resolver_for({"nope.example": OSError("no such host")}))
    assert info.value.code == "dns_failure"
    with pytest.raises(UnsafeURLError) as info:
        await resolve_public_ips("empty.example", 80, resolver_for({"empty.example": []}))
    assert info.value.code == "dns_failure"


# ------------------------------------------------------------------------------------------------ the safe client


def client_with(handler, resolver=None, **settings):
    resolver = resolver or resolver_for({"example.com": [PUBLIC_IP], "other.example": ["93.184.216.35"]})
    return SafeHttpClient(make_settings(**settings), resolver=resolver, transport=httpx.MockTransport(handler)), resolver


def html_response(body="<html><body>ok</body></html>", **kw):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **kw.pop("headers", {})}, content=body.encode() if isinstance(body, str) else body, **kw)


@pytest.mark.anyio
async def test_the_connection_goes_to_the_validated_ip_with_the_real_name_in_host_and_sni():
    seen = {}

    def handler(request):
        seen.update(host_in_url=request.url.host, host_header=request.headers["host"], sni=request.extensions.get("sni_hostname"), path=request.url.raw_path.decode())
        return html_response()

    client, _ = client_with(handler)
    response = await client.get("https://example.com/page?a=1")
    assert response.status == 200
    assert seen == {"host_in_url": PUBLIC_IP, "host_header": "example.com", "sni": "example.com", "path": "/page?a=1"}
    await client.aclose()


@pytest.mark.anyio
async def test_dns_rebinding_cannot_redirect_the_request():
    """DNS gives a public address for the check; a rebinding attacker would then answer with 127.0.0.1 for the connection.
    The client never resolves again: it connects to the address it validated."""
    answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    connected_to = []

    async def resolver(host, port):
        return next(answers)

    def handler(request):
        connected_to.append(request.url.host)
        return html_response()

    client = SafeHttpClient(make_settings(), resolver=resolver, transport=httpx.MockTransport(handler))
    await client.get("https://rebind.example/")
    assert connected_to == [PUBLIC_IP]  # not 127.0.0.1
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_to_a_private_ip_literal_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:80/admin"})

    client, _ = client_with(handler)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "blocked_address"
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_to_the_cloud_metadata_address_is_blocked():
    def handler(request):
        return httpx.Response(301, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    client, _ = client_with(handler)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "blocked_address"
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_to_an_internal_hostname_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://localhost/secret"})

    client, _ = client_with(handler)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "blocked_host"
    await client.aclose()


@pytest.mark.anyio
async def test_every_redirect_hop_is_resolved_and_checked_again():
    """The first host is fine; the host it redirects to resolves to a private address."""

    def handler(request):
        return httpx.Response(302, headers={"location": "https://sneaky.example/next"})

    resolver = resolver_for({"example.com": [PUBLIC_IP], "sneaky.example": ["192.168.0.10"]})
    client, _ = client_with(handler, resolver)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "blocked_address"
    assert resolver.calls == ["example.com", "sneaky.example"]
    await client.aclose()


@pytest.mark.anyio
async def test_redirects_to_other_schemes_are_refused():
    for target in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"):
        client, _ = client_with(lambda request, t=target: httpx.Response(302, headers={"location": t}))
        with pytest.raises(FetchError) as info:
            await client.get("https://example.com/")
        assert info.value.code == "unsupported_scheme"
        await client.aclose()


@pytest.mark.anyio
async def test_a_normal_redirect_is_followed_and_the_final_url_reported():
    def handler(request):
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://other.example/final"})
        return html_response("<html>arrived</html>")

    client, _ = client_with(handler)
    response = await client.get("https://example.com/start")
    assert response.url == "https://other.example/final"
    assert "arrived" in response.text
    await client.aclose()


@pytest.mark.anyio
async def test_relative_redirects_work():
    def handler(request):
        if request.url.raw_path == b"/old":
            return httpx.Response(302, headers={"location": "/new"})
        return html_response()

    client, _ = client_with(handler)
    assert (await client.get("https://example.com/old")).url == "https://example.com/new"
    await client.aclose()


@pytest.mark.anyio
async def test_redirect_loops_stop_at_the_limit():
    hops = []

    def handler(request):
        hops.append(1)
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    client, _ = client_with(handler, max_redirects=3)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "too_many_redirects"
    assert len(hops) == 4  # the first request + 3 redirects
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_without_a_location_is_an_error():
    client, _ = client_with(lambda request: httpx.Response(302))
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "http_error"
    await client.aclose()


@pytest.mark.anyio
async def test_oversized_pages_are_rejected_while_streaming():
    def handler(request):
        return html_response(b"x" * 300_000)

    client, _ = client_with(handler, max_page_size_mb=0.1)  # ~100 KB
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "page_too_large"
    await client.aclose()


@pytest.mark.anyio
async def test_an_oversized_content_length_is_rejected_before_downloading():
    client, _ = client_with(lambda request: html_response(b"tiny", headers={"content-length": "999999999"}), max_page_size_mb=1)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "page_too_large"
    await client.aclose()


@pytest.mark.anyio
async def test_a_compression_bomb_is_stopped_by_the_same_limit():
    bomb = gzip.compress(b"a" * 20_000_000)  # tiny on the wire, 20 MB when decompressed

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}, content=bomb)

    client, _ = client_with(handler, max_page_size_mb=1)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "page_too_large"
    assert len(bomb) < 100_000
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/zip", "video/mp4", "application/octet-stream"])
async def test_unsupported_content_types_are_rejected(content_type):
    client, _ = client_with(lambda request: httpx.Response(200, headers={"content-type": content_type}, content=b"binary"))
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/file")
    assert info.value.code == "unsupported_content_type"
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=iso-8859-1", "application/xhtml+xml", "text/plain"])
async def test_text_content_types_are_accepted(content_type):
    client, _ = client_with(lambda request: httpx.Response(200, headers={"content-type": content_type}, content=b"hello"))
    assert (await client.get("https://example.com/")).status == 200
    await client.aclose()


@pytest.mark.anyio
async def test_timeouts_become_a_fetch_error():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    client, _ = client_with(handler)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "timeout"
    await client.aclose()


@pytest.mark.anyio
async def test_the_overall_time_limit_applies_across_slow_responses():
    import asyncio

    async def slow_resolver(host, port):
        await asyncio.sleep(1.0)
        return [PUBLIC_IP]

    client = SafeHttpClient(make_settings(request_timeout_seconds=0.2), resolver=slow_resolver, transport=httpx.MockTransport(lambda r: html_response()))
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/")
    assert info.value.code == "timeout"
    await client.aclose()


@pytest.mark.anyio
async def test_connection_errors_never_leak_addresses_or_urls():
    def handler(request):
        raise httpx.ConnectError("connect failed to 93.184.216.34:443")

    client, _ = client_with(handler)
    with pytest.raises(FetchError) as info:
        await client.get("https://example.com/secret-path?token=abc")
    assert info.value.code == "connection_error"
    assert "93.184" not in str(info.value) and "token" not in str(info.value)
    await client.aclose()


@pytest.mark.anyio
async def test_the_next_validated_address_is_tried_when_the_first_does_not_connect():
    attempts = []

    def handler(request):
        attempts.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("refused")
        return html_response()

    resolver = resolver_for({"example.com": ["93.184.216.34", "93.184.216.99"]})
    client, _ = client_with(handler, resolver)
    assert (await client.get("https://example.com/")).status == 200
    assert attempts == ["93.184.216.34", "93.184.216.99"]
    await client.aclose()


@pytest.mark.anyio
async def test_ipv6_addresses_are_bracketed_in_the_connection_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return html_response()

    client, _ = client_with(handler, resolver_for({"example.com": ["2606:4700:4700::1111"]}))
    await client.get("https://example.com/x")
    assert seen["url"].startswith("https://[2606:4700:4700::1111]/x")
    await client.aclose()


@pytest.mark.anyio
async def test_http_errors_are_returned_not_raised_so_callers_can_decide():
    client, _ = client_with(lambda request: httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope"))
    assert (await client.get("https://example.com/missing")).status == 404
    await client.aclose()
