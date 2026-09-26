"""The Jonah-compatible endpoints (/search/web, /search/images, /search/videos), news (NewsAPI) and image source finding (Cloud Vision).

The app runs with its REAL provider classes; only the network is fake (httpx.MockTransport routing by host), so these tests check the
exact requests sent upstream and the exact JSON existing Jonah clients receive.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from tests.conftest import FakeFetcher, make_settings

pytestmark = pytest.mark.anyio

GOOGLE_KEY = "AIza-test-google-key-123"
NEWS_KEY = "news-test-key-456789"
VISION_KEY = "vision-test-key-789012"
SHARED = "shared-secret-for-jonah"
AUTH = {"X-Jonah-Key": SHARED}

GOOGLE_WEB = {
    "kind": "customsearch#search",
    "searchInformation": {"totalResults": "2", "searchTime": 0.2},
    "items": [
        {"kind": "customsearch#result", "title": "Python", "link": "https://www.python.org/", "displayLink": "www.python.org", "snippet": "Official site",
         "pagemap": {"cse_image": [{"src": "https://www.python.org/logo.png"}]}},
        {"kind": "customsearch#result", "title": "Tutorial", "link": "https://docs.python.org/3/tutorial/", "displayLink": "docs.python.org", "snippet": "Learn"},
    ],
}  # fmt: skip
QUOTA_DAY = {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per day' of service 'customsearch.googleapis.com'", "status": "RESOURCE_EXHAUSTED"}}
WIKI = {"query": {"search": [{"title": "Python (programming language)", "snippet": "A <span>language</span>"}]}}


class Internet:
    """A fake network: one handler per host; every request is recorded."""

    def __init__(self, **routes):
        self.routes = {host.replace("_", "."): handler for host, handler in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.host)
        if handler is None:
            return httpx.Response(599, text=f"no fake for {request.url.host}")
        return handler(request)

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


def reply(data, status=200, headers=None):
    return lambda request: httpx.Response(status, json=data, headers=headers or {})


def settings(**overrides):
    values = {"google_api_key": GOOGLE_KEY, "google_cx": "cx-test", "app_shared_secret": SHARED, "provider_priority": "searxng,brave,google,wikipedia"}
    values.update(overrides)
    return make_settings(**values)


@pytest.fixture
def start(make_client):
    async def _start(internet: Internet, fetcher=None, **setting_overrides):
        client, app = await make_client(settings(**setting_overrides), real_providers=True, fetcher=fetcher, upstream_transport=httpx.MockTransport(internet))
        return client

    return _start


# ================================================================================================== /search/web


async def test_web_returns_googles_json_unchanged(start):
    internet = Internet(www_googleapis_com=reply(GOOGLE_WEB))
    client = await start(internet)
    response = await client.get("/search/web", params={"q": "python"}, headers=AUTH)
    assert response.status_code == 200 and response.json() == GOOGLE_WEB  # pagemap etc. included: Jonah's Shopping tab reads it
    sent = internet.to("www.googleapis.com")[0].url.params
    assert (sent["q"], sent["cx"], sent["key"]) == ("python", "cx-test", GOOGLE_KEY) and "searchType" not in sent and "num" not in sent


async def test_both_the_jonah_header_and_bearer_keys_are_accepted(start):
    client = await start(Internet(www_googleapis_com=reply(GOOGLE_WEB)), search_api_key="bearer-key-abcdef")
    assert (await client.get("/search/web", params={"q": "x"}, headers={"X-Jonah-Key": SHARED})).status_code == 200
    assert (await client.get("/search/web", params={"q": "x"}, headers={"Authorization": "Bearer bearer-key-abcdef"})).status_code == 200
    assert (await client.get("/search/web", params={"q": "x"}, headers={"Authorization": f"Bearer {SHARED}"})).status_code == 200
    assert (await client.get("/search/web", params={"q": "x"}, headers={"X-Jonah-Key": "wrong"})).status_code == 401
    assert (await client.get("/search/web", params={"q": "x"})).status_code == 401


async def test_a_missing_query_is_a_400_in_the_original_format(start):
    client = await start(Internet())
    for params in ({}, {"q": "   "}):
        response = await client.get("/search/web", params=params, headers=AUTH)
        assert response.status_code == 400
        assert response.json() == {"error": {"message": "Missing required query parameter 'q'", "upstream_status": None}}


async def test_when_googles_quota_is_spent_other_providers_answer_in_googles_format(start):
    internet = Internet(www_googleapis_com=reply(QUOTA_DAY, 429), en_wikipedia_org=reply(WIKI))
    client = await start(internet, enable_wikipedia=True)
    response = await client.get("/search/web", params={"q": "python language"}, headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    item = data["items"][0]
    assert item["title"] == "Python (programming language)" and item["link"] == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert item["displayLink"] == "en.wikipedia.org" and item["snippet"] == "A language"
    assert data["searchInformation"]["totalResults"] == "1"
    assert data["jonah"]["fallback"] is True and data["jonah"]["provider"] == "wikipedia"
    assert "quota_exceeded" in data["jonah"]["reason"] and "429" in data["jonah"]["reason"]
    assert GOOGLE_KEY not in response.text

    await client.get("/search/web", params={"q": "another search"}, headers=AUTH)
    assert len(internet.to("www.googleapis.com")) == 1  # a spent daily quota is not asked again


async def test_with_no_other_provider_the_original_error_comes_back(start):
    client = await start(Internet(www_googleapis_com=reply(QUOTA_DAY, 429)))
    response = await client.get("/search/web", params={"q": "python"}, headers=AUTH)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["upstream_status"] == 429
    assert error["message"].startswith("Search request failed: upstream HTTP 429 - Quota exceeded for quota metric 'Queries' and limit 'Queries per day'")

    again = (await client.get("/search/web", params={"q": "python"}, headers=AUTH)).json()["error"]
    assert again["upstream_status"] == 429 and "Queries per day" in again["message"] and "not retried for about" in again["message"]


async def test_google_not_configured(start):
    wiki = Internet(en_wikipedia_org=reply(WIKI))
    client = await start(wiki, google_api_key=None, enable_wikipedia=True)
    data = (await client.get("/search/web", params={"q": "python"}, headers=AUTH)).json()
    assert data["items"][0]["link"].startswith("https://en.wikipedia.org/") and "not configured" in data["jonah"]["reason"]

    nothing = await start(Internet(), google_api_key=None)
    response = await nothing.get("/search/web", params={"q": "python"}, headers=AUTH)
    assert response.status_code == 503 and "not configured" in response.json()["error"]["message"]


async def test_site_operators_become_domain_filters_in_the_fallback(start):
    searx = {"results": [{"title": "Shoes on Amazon", "url": "https://www.amazon.com/shoes"}, {"title": "Shoe blog", "url": "https://blog.example/shoes"}]}
    internet = Internet(www_googleapis_com=reply(QUOTA_DAY, 429), searx_example=reply(searx))
    client = await start(internet, searxng_url="https://searx.example")
    data = (await client.get("/search/web", params={"q": "running shoes site:amazon.com"}, headers=AUTH)).json()
    assert [i["link"] for i in data["items"]] == ["https://www.amazon.com/shoes"]  # enforced even though SearXNG returned more
    assert internet.to("searx.example")[0].url.params["q"] == "running shoes site:amazon.com"


async def test_the_key_never_leaks_from_a_google_failure(start):
    def leaky(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")  # the URL contains ?key=...

    client = await start(Internet(www_googleapis_com=leaky))
    response = await client.get("/search/web", params={"q": "x"}, headers=AUTH)
    assert response.status_code == 503 and GOOGLE_KEY not in response.text and "ConnectError" in response.text

    bad_key = {"error": {"code": 400, "message": f"API key not valid. key={GOOGLE_KEY}"}}
    client2 = await start(Internet(www_googleapis_com=reply(bad_key, 400)))
    response2 = await client2.get("/search/web", params={"q": "x"}, headers=AUTH)
    assert response2.status_code == 503 and GOOGLE_KEY not in response2.text and "API key not valid" in response2.text

    bare = {"error": {"code": 400, "message": f"The API key {GOOGLE_KEY} was rejected"}}  # the key on its own, not as key=...
    client3 = await start(Internet(www_googleapis_com=reply(bare, 400)))
    response3 = await client3.get("/search/web", params={"q": "x"}, headers=AUTH)
    assert response3.status_code == 503 and GOOGLE_KEY not in response3.text and "[redacted]" in response3.text


async def test_the_key_never_leaks_through_search_errors_either(start):
    bare = {"error": {"code": 400, "message": f"The API key {GOOGLE_KEY} was rejected"}}
    client = await start(Internet(www_googleapis_com=reply(bare, 400)))
    response = await client.get("/search", params={"q": "x"}, headers=AUTH)
    assert response.json()["errors"][0]["provider"] == "google" and GOOGLE_KEY not in response.text
    assert GOOGLE_KEY not in (await client.get("/providers", headers=AUTH)).text


async def test_google_results_are_never_cached(start):
    internet = Internet(www_googleapis_com=reply(GOOGLE_WEB))
    client = await start(internet)
    for _ in range(2):
        await client.get("/search/web", params={"q": "python"}, headers=AUTH)
        await client.get("/search", params={"q": "python", "provider": "google"}, headers=AUTH)
    assert len(internet.to("www.googleapis.com")) == 4  # Google's terms: no cached copies


# =============================================================================================== /search/images


async def test_images_same_request_as_before_and_json_unchanged(start):
    images = {"items": [{"title": "Cat", "link": "https://img.example/cat.jpg", "image": {"contextLink": "https://cats.example/page", "thumbnailLink": "https://t.example/c.jpg"}}]}
    internet = Internet(www_googleapis_com=reply(images))
    client = await start(internet)
    response = await client.get("/search/images", params={"q": "cat"}, headers=AUTH)
    assert response.json() == images
    sent = internet.to("www.googleapis.com")[0].url.params
    assert (sent["q"], sent["searchType"], sent["num"]) == ("cat", "image", "10")


async def test_image_search_failures_use_the_original_format_and_have_no_fallback(start):
    disabled = {"error": {"code": 403, "message": "Custom Search API has not been used in project 123 before or it is disabled."}}
    internet = Internet(www_googleapis_com=reply(disabled, 403), en_wikipedia_org=reply(WIKI))
    client = await start(internet, enable_wikipedia=True)
    response = await client.get("/search/images", params={"q": "cat"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"] == {"message": "Image search request failed: upstream HTTP 403 - Custom Search API has not been used in project 123 before or it is disabled.", "upstream_status": 403}
    assert internet.to("en.wikipedia.org") == []


# =============================================================================================== /search/videos


async def test_videos_try_youtube_first(start):
    internet = Internet(www_googleapis_com=reply(GOOGLE_WEB))
    client = await start(internet)
    assert (await client.get("/search/videos", params={"q": "lofi"}, headers=AUTH)).json() == GOOGLE_WEB
    assert [r.url.params["q"] for r in internet.to("www.googleapis.com")] == ["lofi site:youtube.com"]


async def test_videos_then_try_video_watch_like_jonahs_page(start):
    def google(request):
        return httpx.Response(200, json={"kind": "customsearch#search"} if "youtube" in request.url.params["q"] else GOOGLE_WEB)

    internet = Internet(www_googleapis_com=google)
    client = await start(internet)
    assert (await client.get("/search/videos", params={"q": "lofi"}, headers=AUTH)).json() == GOOGLE_WEB
    assert [r.url.params["q"] for r in internet.to("www.googleapis.com")] == ["lofi site:youtube.com", "lofi video watch"]


async def test_video_fallback_only_returns_youtube(start):
    searx = {"results": [{"title": "Lofi mix", "url": "https://www.youtube.com/watch?v=abcdefghijk"}, {"title": "Lofi article", "url": "https://news.example/lofi"}]}
    client = await start(Internet(www_googleapis_com=reply(QUOTA_DAY, 429), searx_example=reply(searx)), searxng_url="https://searx.example")
    data = (await client.get("/search/videos", params={"q": "lofi"}, headers=AUTH)).json()
    assert [i["link"] for i in data["items"]] == ["https://www.youtube.com/watch?v=abcdefghijk"] and data["jonah"]["provider"] == "searxng"


# ============================================================================================ news (NewsAPI)


HEADLINES = {"status": "ok", "totalResults": 1, "articles": [{"source": {"id": "the-hindu", "name": "The Hindu"}, "title": "Headline", "url": "https://thehindu.com/a"}]}
EMPTY = {"status": "ok", "totalResults": 0, "articles": []}
TECH = {"status": "ok", "totalResults": 1, "articles": [{"source": {"id": None, "name": "Tech"}, "title": "Tech story", "url": "https://tech.example/a"}]}


async def test_headlines_same_request_as_before_and_json_unchanged(start):
    internet = Internet(newsapi_org=reply(HEADLINES))
    client = await start(internet, news_api_key=NEWS_KEY)
    response = await client.get("/news/headlines", params={"country": "in"}, headers=AUTH)
    assert response.status_code == 200 and response.json() == HEADLINES
    sent = internet.to("newsapi.org")[0]
    assert sent.url.path == "/v2/top-headlines" and dict(sent.url.params) == {"country": "in", "pageSize": "20"}
    assert sent.headers["x-api-key"] == NEWS_KEY and NEWS_KEY not in str(sent.url)  # the key is in a header, never in the URL


async def test_headlines_fall_back_to_the_newest_technology_articles(start):
    def news(request):
        return httpx.Response(200, json=EMPTY if request.url.path.endswith("top-headlines") else TECH)

    internet = Internet(newsapi_org=news)
    client = await start(internet, news_api_key=NEWS_KEY)
    assert (await client.get("/news/headlines", headers=AUTH)).json() == TECH  # default country from NEWS_DEFAULT_COUNTRY ('in')
    first, second = internet.to("newsapi.org")
    assert first.url.params["country"] == "in"
    assert second.url.path == "/v2/everything" and dict(second.url.params) == {"q": "technology", "pageSize": "20", "sortBy": "publishedAt"}


async def test_headlines_by_category(start):
    def news(request):
        return httpx.Response(200, json=EMPTY if request.url.path.endswith("top-headlines") else TECH)

    internet = Internet(newsapi_org=news)
    client = await start(internet, news_api_key=NEWS_KEY)
    await client.get("/news/headlines", params={"country": "US", "category": "Sports"}, headers=AUTH)
    first, second = internet.to("newsapi.org")
    assert (first.url.params["country"], first.url.params["category"]) == ("us", "sports") and second.url.params["q"] == "sports"


async def test_news_is_cached_to_save_the_daily_allowance(start):
    internet = Internet(newsapi_org=reply(HEADLINES))
    client = await start(internet, news_api_key=NEWS_KEY)
    for _ in range(3):
        await client.get("/news/headlines", params={"country": "in"}, headers=AUTH)
    assert len(internet.to("newsapi.org")) == 1


async def test_news_errors_use_the_original_format(start):
    invalid = {"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid or incorrect. Check your key, or go to https://newsapi.org to create a free API key."}
    client = await start(Internet(newsapi_org=reply(invalid, 401)), news_api_key=NEWS_KEY)
    response = await client.get("/news/headlines", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"] == {"message": "News request failed: upstream HTTP 401 - " + invalid["message"], "upstream_status": 401}


async def test_newsapis_daily_limit_is_not_hammered(start):
    limited = {"status": "error", "code": "rateLimited", "message": "You have made too many requests recently. Developer accounts are limited to 100 requests over a 24 hour period."}
    internet = Internet(newsapi_org=reply(limited, 429))
    client = await start(internet, news_api_key=NEWS_KEY)
    assert (await client.get("/news/headlines", params={"country": "in"}, headers=AUTH)).status_code == 503
    second = await client.get("/news/headlines", params={"country": "us"}, headers=AUTH)
    assert second.status_code == 503 and "not retried" in second.json()["error"]["message"]
    assert len(internet.to("newsapi.org")) == 1
    providers = {p["name"]: p for p in (await client.get("/providers", headers=AUTH)).json()}
    assert providers["newsapi"]["status"] == "cooling_down" and providers["newsapi"]["last_error"] == "quota_exceeded"


async def test_news_not_configured_and_bad_parameters(start):
    client = await start(Internet())
    missing = await client.get("/news/headlines", headers=AUTH)
    assert missing.status_code == 503 and "NEWS_API_KEY" in missing.json()["error"]["message"]
    configured = await start(Internet(), news_api_key=NEWS_KEY)
    for params in ({"country": "india"}, {"country": "i1"}, {"category": "gossip"}):
        response = await configured.get("/news/headlines", params=params, headers=AUTH)
        assert response.status_code == 400 and response.json()["error"]["upstream_status"] is None


async def test_news_search_passes_the_newsapi_parameters(start):
    internet = Internet(newsapi_org=reply(TECH))
    client = await start(internet, news_api_key=NEWS_KEY)
    params = {"q": "chip  exports", "sources": "reuters, bbc-news", "excludeDomains": "spam.example", "language": "EN", "sortBy": "publishedAt", "from": "2026-09-01", "pageSize": "5"}
    assert (await client.get("/news/search", params=params, headers=AUTH)).json() == TECH
    sent = dict(internet.to("newsapi.org")[0].url.params)
    assert internet.to("newsapi.org")[0].url.path == "/v2/everything"
    assert sent == {"q": "chip exports", "sources": "reuters,bbc-news", "excludeDomains": "spam.example", "language": "en", "sortBy": "publishedAt", "from": "2026-09-01", "page": "1", "pageSize": "5"}


@pytest.mark.parametrize(
    "params",
    [{}, {"q": "x", "sortBy": "newest"}, {"q": "x", "from": "last week"}, {"q": "x", "sources": "bad source!"}, {"q": "x", "domains": "not a domain"}, {"q": "x", "language": "english"}],
)
async def test_news_search_validation(start, params):
    client = await start(Internet(), news_api_key=NEWS_KEY)
    assert (await client.get("/news/search", params=params, headers=AUTH)).status_code == 400


async def test_news_sources_is_the_source_finder(start):
    sources = {"status": "ok", "sources": [{"id": "the-hindu", "name": "The Hindu", "category": "general", "country": "in", "url": "https://www.thehindu.com"}]}
    internet = Internet(newsapi_org=reply(sources))
    client = await start(internet, news_api_key=NEWS_KEY)
    assert (await client.get("/news/sources", params={"country": "in", "category": "general"}, headers=AUTH)).json() == sources
    sent = internet.to("newsapi.org")[0]
    assert sent.url.path == "/v2/top-headlines/sources" and dict(sent.url.params) == {"country": "in", "category": "general"}


# ==================================================================================== image source (Vision)


DETECTION = {
    "responses": [{
        "webDetection": {
            "webEntities": [{"entityId": "/m/02j81", "score": 0.93, "description": "Eiffel Tower"}, {"entityId": "/m/x", "score": 0.1}],
            "fullMatchingImages": [{"url": "https://img.example/eiffel.jpg"}, {"url": "ftp://old.example/e.jpg"}],
            "partialMatchingImages": [{"url": "https://img.example/eiffel-crop.jpg"}],
            "pagesWithMatchingImages": [
                {"url": "https://travel.example/paris", "pageTitle": "Visiting the <b>Eiffel Tower</b>", "fullMatchingImages": [{"url": "https://img.example/eiffel.jpg"}]},
                {"url": "javascript:alert(1)", "pageTitle": "bad"},
            ],
            "visuallySimilarImages": [{"url": "https://img.example/similar.jpg"}],
            "bestGuessLabels": [{"label": "eiffel tower", "languageCode": "en"}],
        }
    }]
}  # fmt: skip
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def vision_calls(internet: Internet) -> list[dict]:
    return [json.loads(r.content) for r in internet.to("vision.googleapis.com")]


async def test_image_source_by_url(start):
    internet = Internet(vision_googleapis_com=reply(DETECTION))
    client = await start(internet, google_vision_api_key=VISION_KEY)
    response = await client.post("/search/image-source", json={"image_url": "https://img.example/unknown.jpg", "max_results": 5}, headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["best_guess_labels"] == ["eiffel tower"]
    assert data["entities"] == [{"description": "Eiffel Tower", "score": 0.93}]
    assert data["pages"] == [{"url": "https://travel.example/paris", "title": "Visiting the Eiffel Tower", "full_matching_images": ["https://img.example/eiffel.jpg"], "partial_matching_images": []}]
    assert data["full_matching_images"] == ["https://img.example/eiffel.jpg"]  # the ftp:// URL is dropped
    assert data["image"] == {"url": "https://img.example/unknown.jpg", "fetched_by_server": False}
    assert data["metadata"]["provider"] == "google_vision" and data["metadata"]["content_trust"] == "untrusted"
    request = internet.to("vision.googleapis.com")[0]
    assert request.url.path == "/v1/images:annotate" and request.url.params["key"] == VISION_KEY
    assert vision_calls(internet)[0] == {"requests": [{"image": {"source": {"imageUri": "https://img.example/unknown.jpg"}}, "features": [{"type": "WEB_DETECTION", "maxResults": 5}]}]}
    assert VISION_KEY not in response.text


async def test_the_google_key_is_used_when_there_is_no_separate_vision_key(start):
    internet = Internet(vision_googleapis_com=reply(DETECTION))
    client = await start(internet)
    await client.post("/search/image-source", json={"image_url": "https://img.example/a.jpg"}, headers=AUTH)
    assert internet.to("vision.googleapis.com")[0].url.params["key"] == GOOGLE_KEY


async def test_image_source_by_upload(start):
    internet = Internet(vision_googleapis_com=reply(DETECTION))
    client = await start(internet, google_vision_api_key=VISION_KEY)
    encoded = base64.b64encode(PNG).decode()
    response = await client.post("/search/image-source", json={"image_base64": "data:image/png;base64," + encoded}, headers=AUTH)
    assert response.status_code == 200 and response.json()["image"] == {"uploaded_bytes": len(PNG)}
    assert vision_calls(internet)[0]["requests"][0]["image"] == {"content": encoded}


async def test_when_google_cannot_download_the_image_this_server_does(start):
    calls = []

    def vision(request):
        body = json.loads(request.content)
        calls.append(body)
        if "source" in body["requests"][0]["image"]:
            return httpx.Response(200, json={"responses": [{"error": {"code": 7, "message": "We can not access the URL currently. Please download the content and pass it in."}}]})
        return httpx.Response(200, json=DETECTION)

    url = "https://shy.example/photo.png"
    fetcher = FakeFetcher(images={url: PNG})
    client = await start(Internet(vision_googleapis_com=vision), fetcher=fetcher, google_vision_api_key=VISION_KEY)
    response = await client.post("/search/image-source", json={"image_url": url}, headers=AUTH)
    assert response.status_code == 200 and response.json()["image"] == {"url": url, "fetched_by_server": True}
    assert fetcher.calls == [url] and calls[1]["requests"][0]["image"] == {"content": base64.b64encode(PNG).decode()}

    blocked = FakeFetcher(errors={url: "robots_disallowed"})
    client2 = await start(Internet(vision_googleapis_com=vision), fetcher=blocked, google_vision_api_key=VISION_KEY)
    refused = await client2.post("/search/image-source", json={"image_url": url}, headers=AUTH)
    assert refused.status_code == 400 and "robots_disallowed" in refused.json()["error"]["message"]


async def test_a_bad_image_is_a_400_and_a_disabled_api_is_the_original_503(start):
    bad = {"responses": [{"error": {"code": 3, "message": "Bad image data."}}]}
    client = await start(Internet(vision_googleapis_com=reply(bad)), google_vision_api_key=VISION_KEY)
    response = await client.post("/search/image-source", json={"image_base64": base64.b64encode(b"not an image").decode()}, headers=AUTH)
    assert response.status_code == 400 and "Bad image data" in response.json()["error"]["message"]

    disabled = {"error": {"code": 403, "message": "Cloud Vision API has not been used in project 123 before or it is disabled.", "status": "PERMISSION_DENIED"}}
    client2 = await start(Internet(vision_googleapis_com=reply(disabled, 403)), google_vision_api_key=VISION_KEY)
    response2 = await client2.post("/search/image-source", json={"image_url": "https://img.example/a.jpg"}, headers=AUTH)
    assert response2.status_code == 503
    assert response2.json()["error"] == {"message": "Image source request failed: upstream HTTP 403 - " + disabled["error"]["message"], "upstream_status": 403}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"image_url": "https://a.example/x.jpg", "image_base64": "AAAA"},
        {"image_url": "ftp://a.example/x.jpg"},
        {"image_url": "file:///etc/passwd"},
        {"image_base64": "***not base64***"},
    ],
)
async def test_image_source_input_validation(start, body):
    internet = Internet(vision_googleapis_com=reply(DETECTION))
    client = await start(internet, google_vision_api_key=VISION_KEY)
    assert (await client.post("/search/image-source", json=body, headers=AUTH)).status_code == 400
    assert internet.to("vision.googleapis.com") == []


async def test_images_over_the_size_limit_are_refused_before_decoding(start):
    internet = Internet(vision_googleapis_com=reply(DETECTION))
    client = await start(internet, google_vision_api_key=VISION_KEY, max_image_mb=0.001)  # about 1 KB
    big = base64.b64encode(b"\x00" * 4096).decode()
    response = await client.post("/search/image-source", json={"image_base64": big}, headers=AUTH)
    assert response.status_code == 400 and "larger than" in response.json()["error"]["message"]
    assert internet.to("vision.googleapis.com") == []


async def test_vision_not_configured(start):
    client = await start(Internet(), google_api_key=None)
    response = await client.post("/search/image-source", json={"image_url": "https://img.example/a.jpg"}, headers=AUTH)
    assert response.status_code == 503 and "not configured" in response.json()["error"]["message"]


# ================================================================================================= body limit


async def test_oversized_request_bodies_are_refused(start):
    client = await start(Internet(), google_vision_api_key=VISION_KEY, max_request_body_mb=0.01)  # about 10 KB
    big = {"image_base64": "A" * 20000}
    declared = await client.post("/search/image-source", json=big, headers=AUTH)
    assert declared.status_code == 413 and declared.headers["x-request-id"]

    async def chunks():  # no Content-Length: the size is counted as the body arrives
        payload = json.dumps(big).encode()
        for start_at in range(0, len(payload), 4096):
            yield payload[start_at:start_at + 4096]

    streamed = await client.post("/search/image-source", content=chunks(), headers={**AUTH, "Content-Type": "application/json"})
    assert streamed.status_code == 413
    small = await client.post("/search/image-source", json={"image_url": "https://img.example/a.jpg"}, headers=AUTH)
    assert small.status_code != 413
