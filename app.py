"""
Jonah News/Search Proxy
========================
A small Flask backend whose only job is to hold the NewsAPI and Google
Custom Search credentials server-side, so they never ship inside the
iOS binary. It mirrors exactly what GoogleSearchService.swift and
NewsService.swift used to do locally — same upstream endpoints, same
query parameters, same response shapes — so the Swift-side Codable
models (`NewsAPIResponse`, `CSEResponse`) decode this without any
change to their field definitions.

Endpoints
---------
GET /
    Human-facing landing route. Not called by the app — just so a
    person (or a search engine) hitting the bare domain sees something
    other than a bare 404. Returns a small plain-text description and
    links to /health.

GET /health
    Liveness check. Returns {"status": "ok"}.

GET /news/headlines?country=in
    Mirrors NewsService.fetchTopHeadlines: tries NewsAPI's
    top-headlines for `country` first; if that comes back with zero
    articles, falls back to /everything?q=technology&sortBy=publishedAt
    — identical fallback behavior to the old client-side logic, just
    moved server-side. Returns NewsAPI's raw JSON body unchanged
    (extra fields NewsAPI includes that Swift's model doesn't use are
    simply ignored by Codable, same as before).

GET /search/web?q=...
GET /search/images?q=...
    Mirrors GoogleSearchService.searchWeb / .searchImages. Proxies to
    Google's Custom Search JSON API with the server-held key + cx,
    returns Google's raw JSON body unchanged.

Errors
------
When Google / NewsAPI answers with an error (quota used up, invalid key,
API not enabled, ...) or cannot be reached, the endpoints return

    HTTP 503   {"error": {"message": "Search request failed: upstream HTTP 429 - <Google's own message>",
                          "upstream_status": 429}}

and the same reason is written to the server log (Render -> Logs).

Two deliberate choices, both fixes:
  * The response never contains str(exception). requests' HTTPError text
    is "403 Client Error: Forbidden for url: https://...?key=<YOUR KEY>&cx=...",
    i.e. it includes the API key. Only the upstream status and the upstream
    service's own message are returned, with any key=... / secret scrubbed.
  * The status is 503, not 502. Cloudflare (which fronts this service)
    REPLACES the body of an origin 502/504 with its own "error code: 502"
    page, so the reason was invisible to the app and to anyone debugging.
    (If you ever see a bare "error code: 503" instead, change
    UPSTREAM_ERROR_STATUS below to 500.)

Auth
----
Optional shared-secret header check (APP_SHARED_SECRET). This is
NOT a real secret in the cryptographic sense — it still ships inside
the iOS binary and can be extracted the same way the old API keys
could. Its only purpose is to stop anonymous/automated scanning from
burning your NewsAPI/Google quota if someone finds this URL; it does
not, and cannot, make the endpoint attacker-proof. If you don't set
APP_SHARED_SECRET, the check is skipped entirely and the endpoints are
open to anyone with the URL — fine for testing, worth turning on
before you rely on this for a shipped app.

Environment variables (set these on Render, never commit them)
----------------------------------------------------------------
NEWS_API_KEY          required  — your newsapi.org key
GOOGLE_API_KEY        required  — your Google Cloud API key
GOOGLE_CSE_ID         required  — your Custom Search Engine ID (cx)
APP_SHARED_SECRET     optional  — if set, requests must send it as
                                  the X-Jonah-Key header
"""

import os
import re
import requests
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")
APP_SHARED_SECRET = os.environ.get("APP_SHARED_SECRET")  # optional

NEWSAPI_BASE = "https://newsapi.org/v2"
GOOGLE_CSE_BASE = "https://www.googleapis.com/customsearch/v1"

UPSTREAM_TIMEOUT = 10  # seconds — fail fast rather than hang the app

# Status returned when Google/NewsAPI fails. NOT 502/504: Cloudflare replaces
# those bodies with its own page and the reason is lost (see "Errors" above).
UPSTREAM_ERROR_STATUS = 503


def _require_shared_secret():
    """No-op if APP_SHARED_SECRET isn't configured (e.g. local testing).
    Once set, every request below must include a matching X-Jonah-Key
    header or gets a 401."""
    if not APP_SHARED_SECRET:
        return
    provided = request.headers.get("X-Jonah-Key")
    if provided != APP_SHARED_SECRET:
        abort(401, description="Missing or invalid X-Jonah-Key header")


def _require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        abort(500, description=f"Server is missing required environment variables: {', '.join(missing)}")


def _scrub(text):
    """Remove anything secret from text that is about to leave the server or
    be logged: key=... / apiKey=... query parameters, and the literal values
    of this service's own keys and shared secret."""
    text = re.sub(r"(key|apiKey)=[^&\s\"']+", r"\1=[redacted]", str(text), flags=re.IGNORECASE)
    for secret in (GOOGLE_API_KEY, NEWS_API_KEY, APP_SHARED_SECRET):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _upstream_error(what, resp=None, exc=None):
    """A failed upstream call as a SAFE, readable JSON error (see "Errors" in
    the module docstring). Pass the upstream response (`resp`) when there was
    one, or the exception (`exc`) when the request itself failed."""
    status = resp.status_code if resp is not None else None
    reason = ""
    if resp is not None:
        try:
            body = resp.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    reason = error.get("message") or ""
                elif isinstance(error, str):
                    reason = error
                reason = reason or body.get("message") or ""
        except ValueError:
            reason = (resp.text or "")[:200]
    elif exc is not None:
        # The exception CLASS only (Timeout, ConnectionError, ...). Its text
        # contains the request URL, and with it the API key.
        reason = "upstream reply was not JSON" if isinstance(exc, ValueError) else type(exc).__name__
    reason = _scrub(reason)
    app.logger.error("%s failed: upstream status=%s reason=%s", what, status, reason)
    message = f"{what} failed"
    if status:
        message += f": upstream HTTP {status}"
    if reason:
        message += f" - {reason}"
    return jsonify({"error": {"message": message[:400], "upstream_status": status}}), UPSTREAM_ERROR_STATUS


@app.route("/", methods=["GET"])
def root():
    # Not used by the app itself — this exists purely so a human (or
    # a search-engine crawler) landing on the bare domain sees a real,
    # deliberate response instead of a bare 404. Kept intentionally
    # tiny and static: no upstream calls, no auth check, can't fail.
    return (
        "Jonah backend is running.\n\n"
        "This service is a private API proxy for the Jonah iOS app — "
        "it has no public UI.\n\n"
        "Liveness check: /health\n",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/news/headlines", methods=["GET"])
def news_headlines():
    _require_shared_secret()
    _require_env("NEWS_API_KEY")

    country = request.args.get("country", "in")

    try:
        primary = requests.get(
            f"{NEWSAPI_BASE}/top-headlines",
            params={"country": country, "pageSize": 20, "apiKey": NEWS_API_KEY},
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return _upstream_error("News request", exc=e)
    if not primary.ok:
        return _upstream_error("News request", resp=primary)
    try:
        primary_json = primary.json()
    except ValueError as e:
        return _upstream_error("News request", exc=e)

    if primary_json.get("articles"):
        return jsonify(primary_json)

    # Same fallback the old NewsService.swift did when top-headlines
    # for this country comes back empty.
    try:
        fallback = requests.get(
            f"{NEWSAPI_BASE}/everything",
            params={
                "q": "technology",
                "pageSize": 20,
                "sortBy": "publishedAt",
                "apiKey": NEWS_API_KEY,
            },
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return _upstream_error("News request", exc=e)
    if not fallback.ok:
        return _upstream_error("News request", resp=fallback)
    try:
        return jsonify(fallback.json())
    except ValueError as e:
        return _upstream_error("News request", exc=e)


@app.route("/search/web", methods=["GET"])
def search_web():
    _require_shared_secret()
    _require_env("GOOGLE_API_KEY", "GOOGLE_CSE_ID")

    query = request.args.get("q", "")
    if not query:
        abort(400, description="Missing required query parameter 'q'")

    try:
        resp = requests.get(
            GOOGLE_CSE_BASE,
            params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query},
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return _upstream_error("Search request", exc=e)
    if not resp.ok:
        return _upstream_error("Search request", resp=resp)
    try:
        return jsonify(resp.json())
    except ValueError as e:
        return _upstream_error("Search request", exc=e)


@app.route("/search/images", methods=["GET"])
def search_images():
    _require_shared_secret()
    _require_env("GOOGLE_API_KEY", "GOOGLE_CSE_ID")

    query = request.args.get("q", "")
    if not query:
        abort(400, description="Missing required query parameter 'q'")

    try:
        resp = requests.get(
            GOOGLE_CSE_BASE,
            params={
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CSE_ID,
                "q": query,
                "searchType": "image",
                "num": 10,
            },
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return _upstream_error("Image search request", exc=e)
    if not resp.ok:
        return _upstream_error("Image search request", resp=resp)
    try:
        return jsonify(resp.json())
    except ValueError as e:
        return _upstream_error("Image search request", exc=e)


if __name__ == "__main__":
    # Local dev only. On Render, gunicorn runs this via the Procfile/
    # start command instead (see README.md).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
