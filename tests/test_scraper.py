"""robots.txt, page fetching, content extraction and text sanitising."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.scraper.extractor import extract_content
from app.scraper.fetcher import FetchError, PageFetcher
from app.scraper.robots import RobotsChecker, RobotsRules
from app.scraper.sanitizer import sanitize_text, truncate_text
from tests.conftest import ARTICLE_HTML, make_settings

UA = "JonahSearchBot"


# ------------------------------------------------------------------------------------------------- robots rules


def allowed(robots: str, path: str, agent: str = UA) -> bool:
    return RobotsRules.parse(robots).allowed(path, agent)


def test_disallow_all_for_everyone():
    assert allowed("User-agent: *\nDisallow: /", "/anything") is False
    assert allowed("User-agent: *\nDisallow: /", "/") is False


def test_empty_disallow_and_empty_file_allow_everything():
    assert allowed("User-agent: *\nDisallow:", "/x") is True
    assert allowed("", "/x") is True
    assert allowed("# only a comment", "/x") is True


def test_path_prefixes():
    robots = "User-agent: *\nDisallow: /private\nDisallow: /tmp/"
    assert allowed(robots, "/private") is False
    assert allowed(robots, "/private/data") is False
    assert allowed(robots, "/privateer") is False  # prefix match, as the standard defines
    assert allowed(robots, "/tmp/x") is False
    assert allowed(robots, "/public") is True


def test_longest_match_wins_and_allow_beats_disallow_on_a_tie():
    robots = "User-agent: *\nDisallow: /docs/\nAllow: /docs/public/"
    assert allowed(robots, "/docs/secret") is False
    assert allowed(robots, "/docs/public/page") is True
    tie = "User-agent: *\nDisallow: /page\nAllow: /page"
    assert allowed(tie, "/page") is True


def test_wildcards_and_end_anchors():
    robots = "User-agent: *\nDisallow: /*?session=\nDisallow: /*.pdf$\nDisallow: /a*b"
    assert allowed(robots, "/x?session=1") is False
    assert allowed(robots, "/x?other=1") is True
    assert allowed(robots, "/files/report.pdf") is False
    assert allowed(robots, "/files/report.pdf?download=1") is True  # '$' anchors the end
    assert allowed(robots, "/a-long-b") is False
    assert allowed(robots, "/ab") is False
    assert allowed(robots, "/a") is True


def test_the_most_specific_user_agent_group_wins():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: JonahSearchBot\nAllow: /\nDisallow: /admin\n"
    assert allowed(robots, "/page") is True  # our own group overrides '*'
    assert allowed(robots, "/admin/x") is False
    other = "User-agent: *\nAllow: /\n\nUser-agent: SomeOtherBot\nDisallow: /\n"
    assert allowed(other, "/page") is True  # a group for a different bot does not apply to us
    assert allowed(other, "/page", agent="SomeOtherBot") is False


def test_several_user_agent_lines_share_one_group():
    robots = "User-agent: alphabot\nUser-agent: JonahSearchBot\nDisallow: /nope\n"
    assert allowed(robots, "/nope") is False
    assert allowed(robots, "/ok") is True


def test_case_comments_and_whitespace_are_tolerated():
    robots = "USER-AGENT: *   # everyone\n  disallow : /Secret  # keep out\n"
    assert allowed(robots, "/Secret") is False
    assert allowed(robots, "/secret") is True  # paths are case-sensitive


# ------------------------------------------------------------------------------------------ robots + fetching


def robots_getter(status: int, body: str = "", calls: list | None = None, error: Exception | None = None):
    async def get(url):
        if calls is not None:
            calls.append(url)
        if error:
            raise error
        return status, body

    return get


@pytest.mark.anyio
async def test_robots_txt_status_codes_follow_the_standard():
    disallow = "User-agent: *\nDisallow: /"
    assert (await RobotsChecker(robots_getter(200, disallow), UA).allowed("https://e.com/x")).allowed is False
    assert (await RobotsChecker(robots_getter(404), UA).allowed("https://e.com/x")).allowed is True  # no robots.txt = no restrictions
    assert (await RobotsChecker(robots_getter(403), UA).allowed("https://e.com/x")).allowed is True  # 4xx: unavailable = allowed
    unavailable = await RobotsChecker(robots_getter(503), UA).allowed("https://e.com/x")
    assert (unavailable.allowed, unavailable.reason) == (False, "robots_unavailable")  # 5xx: assume the site does not want crawlers
    slow_down = await RobotsChecker(robots_getter(429), UA).allowed("https://e.com/x")
    assert (slow_down.allowed, slow_down.reason) == (False, "robots_unavailable")  # 429 is "back off", not "no robots.txt"
    errored = await RobotsChecker(robots_getter(0, error=OSError("boom")), UA).allowed("https://e.com/x")
    assert (errored.allowed, errored.reason) == (False, "robots_unavailable")


@pytest.mark.anyio
async def test_fail_open_can_be_chosen_explicitly():
    assert (await RobotsChecker(robots_getter(503), UA, fail_open=True).allowed("https://e.com/x")).allowed is True


@pytest.mark.anyio
async def test_disallowed_reason_and_query_strings():
    checker = RobotsChecker(robots_getter(200, "User-agent: *\nDisallow: /search"), UA)
    decision = await checker.allowed("https://e.com/search?q=x")
    assert (decision.allowed, decision.reason) == (False, "robots_disallowed")
    assert (await checker.allowed("https://e.com/about")).allowed is True


@pytest.mark.anyio
async def test_robots_txt_is_downloaded_once_per_origin_even_for_simultaneous_requests():
    calls: list[str] = []

    async def slow_get(url):
        calls.append(url)
        await asyncio.sleep(0.05)
        return 200, "User-agent: *\nDisallow: /nope"

    checker = RobotsChecker(slow_get, UA)
    decisions = await asyncio.gather(*(checker.allowed(f"https://e.com/page{i}") for i in range(6)), checker.allowed("https://other.com/x"))
    assert all(d.allowed for d in decisions)
    assert sorted(calls) == ["https://e.com/robots.txt", "https://other.com/robots.txt"]  # one each, not seven
    await checker.allowed("https://e.com/again")
    assert len(calls) == 2  # and cached afterwards


@pytest.mark.anyio
async def test_robots_cache_expires():
    now = [0.0]
    calls: list[str] = []
    checker = RobotsChecker(robots_getter(200, "", calls), UA, clock=lambda: now[0])
    await checker.allowed("https://e.com/a")
    await checker.allowed("https://e.com/b")
    assert len(calls) == 1
    now[0] = 4000.0  # past the one-hour lifetime
    await checker.allowed("https://e.com/c")
    assert len(calls) == 2


# ------------------------------------------------------------------------------------------------ PageFetcher


PUBLIC_IP = "93.184.216.34"


def page_fetcher(handler, **settings):
    async def resolver(host, port):
        return [PUBLIC_IP]

    return PageFetcher(make_settings(**settings), resolver=resolver, transport=httpx.MockTransport(handler))


def site(robots: str | None, page: httpx.Response | None = None, requests: list | None = None):
    def handler(request):
        if requests is not None:
            requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            if robots is None:
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=robots.encode())
        return page or httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=ARTICLE_HTML.encode())

    return handler


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.anyio
async def test_images_are_downloaded_with_the_same_protections():
    fetcher = page_fetcher(site(None, httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)))
    assert await fetcher.fetch_image("https://example.com/cat.png", 1000) == (PNG, "image/png")
    await fetcher.aclose()

    html_page = page_fetcher(site(None))  # an HTML page where an image was expected
    with pytest.raises(FetchError) as info:
        await html_page.fetch_image("https://example.com/cat.png", 1000)
    assert info.value.code == "unsupported_content_type"
    await html_page.aclose()

    too_big = page_fetcher(site(None, httpx.Response(200, headers={"content-type": "image/png"}, content=PNG * 100)))
    with pytest.raises(FetchError) as info:
        await too_big.fetch_image("https://example.com/cat.png", 1000)
    assert info.value.code == "page_too_large"
    await too_big.aclose()

    disallowed = page_fetcher(site("User-agent: *\nDisallow: /images/", httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)))
    with pytest.raises(FetchError) as info:
        await disallowed.fetch_image("https://example.com/images/cat.png", 1000)
    assert info.value.code == "robots_disallowed"
    await disallowed.aclose()


@pytest.mark.anyio
async def test_image_downloads_refuse_internal_addresses():
    fetcher = page_fetcher(site(None, httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)))
    for url in ("http://169.254.169.254/latest/meta-data/iam.png", "http://127.0.0.1/x.png", "http://localhost/x.png", "file:///etc/passwd"):
        with pytest.raises(FetchError):
            await fetcher.fetch_image(url, 1000)
    await fetcher.aclose()


@pytest.mark.anyio
async def test_a_page_is_fetched_when_robots_allows_it():
    fetcher = page_fetcher(site("User-agent: *\nDisallow: /private"))
    page = await fetcher.fetch("https://example.com/article")
    assert page.status == 200
    assert "Search engines explained" in page.text
    await fetcher.aclose()


@pytest.mark.anyio
async def test_a_page_disallowed_by_robots_is_never_requested():
    requests: list[str] = []
    fetcher = page_fetcher(site("User-agent: *\nDisallow: /private", requests=requests))
    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://example.com/private/report")
    assert info.value.code == "robots_disallowed"
    assert requests == ["/robots.txt"]  # the page itself was not touched
    await fetcher.aclose()


@pytest.mark.anyio
async def test_robots_is_checked_again_after_a_redirect_to_another_site():
    def handler(request):
        host = request.headers["host"]
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=(b"User-agent: *\nDisallow: /" if host == "blocked.example" else b""))
        if host == "example.com":
            return httpx.Response(302, headers={"location": "https://blocked.example/landing"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>x</html>")

    fetcher = page_fetcher(handler)
    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://example.com/go")
    assert info.value.code == "robots_disallowed"
    await fetcher.aclose()


@pytest.mark.anyio
async def test_robots_can_be_switched_off_by_the_operator():
    requests: list[str] = []
    fetcher = page_fetcher(site("User-agent: *\nDisallow: /", requests=requests), respect_robots=False)
    assert (await fetcher.fetch("https://example.com/x")).status == 200
    assert "/robots.txt" not in requests
    await fetcher.aclose()


@pytest.mark.anyio
async def test_an_unreadable_robots_txt_means_do_not_crawl_by_default():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>x</html>")

    fetcher = page_fetcher(handler)
    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://example.com/x")
    assert info.value.code == "robots_unavailable"
    await fetcher.aclose()
    open_fetcher = page_fetcher(handler, robots_fail_open=True)
    assert (await open_fetcher.fetch("https://example.com/x")).status == 200
    await open_fetcher.aclose()


@pytest.mark.anyio
async def test_http_errors_and_a_clear_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, headers={"content-type": "text/html"}, content=b"forbidden")

    fetcher = page_fetcher(handler, user_agent="JonahSearchBot/1.0 (+https://example.org/bot)")
    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://example.com/x")
    assert (info.value.code, info.value.detail) == ("http_error", "403")  # a refusal is reported, never worked around
    assert seen["ua"] == "JonahSearchBot/1.0 (+https://example.org/bot)"
    await fetcher.aclose()


@pytest.mark.anyio
async def test_unsafe_urls_are_refused_before_any_request():
    requests: list[str] = []
    fetcher = page_fetcher(site(None, requests=requests))
    for url, code in (("http://127.0.0.1/", "blocked_address"), ("file:///etc/passwd", "unsupported_scheme"), ("http://localhost/", "blocked_host"), ("http://169.254.169.254/", "blocked_address")):
        with pytest.raises(FetchError) as info:
            await fetcher.fetch(url)
        assert info.value.code == code
    assert requests == []
    await fetcher.aclose()


# ---------------------------------------------------------------------------------------------------- extraction


def test_the_article_is_extracted_and_the_boilerplate_is_not():
    result = extract_content(ARTICLE_HTML, "https://example.com/a", 50000)
    assert "Search engines explained" in result.text
    assert "crawling pages, indexing their content" in result.text
    for junk in ("Accept all cookies", "SECRET_TRACKING_CODE", "About us", "Related links", "Copyright 2026"):
        assert junk not in result.text
    assert result.published_at == "2026-09-20T00:00:00Z"
    assert result.truncated is False


def test_extraction_is_repeatable_the_second_time_gives_the_same_text():
    """Regression: Trafilatura's `deduplicate` keeps GLOBAL state and made a page that was extracted twice come back empty."""
    first = extract_content(ARTICLE_HTML, "https://example.com/a", 50000)
    second = extract_content(ARTICLE_HTML, "https://example.com/a", 50000)
    third = extract_content(ARTICLE_HTML, "https://example.com/other-url", 50000)
    assert first.text and first.text == second.text == third.text


def test_long_pages_are_truncated_at_a_word_boundary():
    result = extract_content(ARTICLE_HTML, "https://example.com/a", 300)
    assert result.truncated is True
    assert len(result.text) <= 300
    assert not result.text.endswith(" ")
    assert result.text.split()[-1].isalpha() or result.text.split()[-1][-1] in ".,"  # cut between words


def test_the_fallback_handles_pages_the_main_extractor_gives_up_on():
    html = "<html><head><title>Tiny</title></head><body><nav>menu menu menu</nav><main><p>" + "Short but real content lives in the main element of this page. " * 3 + "</p></main><footer>foot</footer></body></html>"
    result = extract_content(html, "https://example.com/t", 50000)
    assert "real content lives in the main element" in result.text
    assert "menu" not in result.text and "foot" not in result.text
    assert result.title == "Tiny"


def test_the_fallback_removes_elements_marked_as_banners_and_ads_but_keeps_the_article():
    html = (
        "<html><body><div class='cookie-consent'>We use cookies to improve your experience today for you.</div>"
        "<div class='advert-slot'>Buy our amazing product now with this special offer for all visitors.</div>"
        "<main><p>" + "The real article content that a reader actually came for stays. " * 2 + "</p></main></body></html>"
    )
    result = extract_content(html, "https://example.com/t", 50000)
    assert "real article content" in result.text
    assert "cookies" not in result.text and "amazing product" not in result.text


def test_garbage_and_empty_html_do_not_crash():
    for html in ("", "   ", "<<<<>>>", "not html at all", "<html", "\x00\x01\x02", "<div>" * 5000):
        result = extract_content(html, "https://example.com/x", 1000)
        assert isinstance(result.text, str)


def test_only_the_configured_amount_of_html_is_processed():
    huge = "<html><body><article>" + "<p>" + "A long paragraph of ordinary article text for the extractor. " * 20 + "</p>" * 1 + "</article>" + "<div>x</div>" * 400_000 + "</body></html>"
    assert len(huge) > 4_000_000
    result = extract_content(huge, "https://example.com/x", 5000, max_html_chars=100_000)
    assert "ordinary article text" in result.text  # the content near the top survives; the rest is never parsed


# ----------------------------------------------------------------------------------------------------- sanitiser


def test_invisible_and_control_characters_are_removed():
    dirty = "Hello​ World‮ evil⁦ text﻿­ok\x00\x07 done⁠"
    assert sanitize_text(dirty) == "Hello World evil textok done"


def test_whitespace_is_tidied():
    assert sanitize_text("a   b\t\tc\n\n\n\n\nd  \n e\r\nf") == "a b c\n\nd\n e\nf"
    assert sanitize_text(None) == "" and sanitize_text("") == ""
    assert sanitize_text("non breaking") == "non breaking"


def test_truncate_text_cuts_between_words_and_reports_it():
    text = "one two three four five six seven eight nine ten"
    cut, was_cut = truncate_text(text, 20)
    assert was_cut and cut == "one two three four" and len(cut) <= 20
    assert truncate_text(text, 500) == (text, False)
    assert truncate_text(text, 0) == (text, False)  # 0 = unlimited
