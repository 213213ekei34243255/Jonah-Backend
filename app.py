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
        primary.raise_for_status()
        primary_json = primary.json()

        if primary_json.get("articles"):
            return jsonify(primary_json)

        # Same fallback the old NewsService.swift did when top-headlines
        # for this country comes back empty.
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
        fallback.raise_for_status()
        return jsonify(fallback.json())

    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": f"News request failed: {e}"}), 502


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
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": {"message": f"Search request failed: {e}"}}), 502


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
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": {"message": f"Image search request failed: {e}"}}), 502


if __name__ == "__main__":
    # Local dev only. On Render, gunicorn runs this via the Procfile/
    # start command instead (see README.md).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
