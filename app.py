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

GET /shopping/ebay?q=...
    eBay product search (eBay Browse API, item_summary/search). Returns
    eBay's raw JSON body unchanged: `itemSummaries[]` with title,
    price {value, currency}, image {imageUrl}, itemWebUrl, condition,
    seller, shippingOptions, ... plus total / limit / offset.
    Optional parameters:
      limit=1..200 (default 20)   offset=0..9999
      sort=price | -price | newlyListed | endingSoonest   (default: best match)
      min_price= / max_price=     (in the marketplace's currency)
      condition=new | used
      buying=fixed_price | auction
      marketplace=EBAY_US | EBAY_GB | EBAY_DE | ...  (default EBAY_MARKETPLACE_ID)

GET /shopping/ebay/item/<item_id>
    One item's full details (Browse API getItem), eBay's JSON unchanged.
    item_id is the `itemId` from a search result, e.g. v1|123456789|0.

    eBay needs an OAuth "application access token". The server gets one
    with EBAY_CLIENT_ID + EBAY_CLIENT_SECRET (client-credentials grant),
    keeps it until shortly before it expires (eBay: 2 hours), and gets a
    new one when eBay rejects it. The client never sees any of this.

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
EBAY_CLIENT_ID        for eBay  — "App ID (Client ID)" of your eBay
                                  developer keyset
EBAY_CLIENT_SECRET    for eBay  — "Cert ID (Client Secret)" of the same
                                  keyset
EBAY_ENVIRONMENT      optional  — "production" (default) or "sandbox";
                                  must match the keyset you use
EBAY_MARKETPLACE_ID   optional  — default marketplace, e.g. EBAY_US
                                  (default), EBAY_GB, EBAY_DE, EBAY_AU
EBAY_AFFILIATE_CAMPAIGN_ID  optional — your eBay Partner Network campaign
                                  id: results then also carry
                                  itemAffiliateWebUrl (commission links)
"""

import base64
import os
import re
import threading
import time

import requests
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")
APP_SHARED_SECRET = os.environ.get("APP_SHARED_SECRET")  # optional
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")

NEWSAPI_BASE = "https://newsapi.org/v2"
GOOGLE_CSE_BASE = "https://www.googleapis.com/customsearch/v1"
EBAY_HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_MARKETPLACES = {
    "EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_AU", "EBAY_CA", "EBAY_FR", "EBAY_IT", "EBAY_ES",
    "EBAY_AT", "EBAY_BE", "EBAY_CH", "EBAY_IE", "EBAY_NL", "EBAY_PL", "EBAY_HK", "EBAY_SG", "EBAY_MY", "EBAY_PH",
}
EBAY_SORTS = {"price", "-price", "newlyListed", "endingSoonest"}

# The eBay application token, shared by this process's threads: {"value": str, "expires_at": epoch seconds}.
_ebay_token = {}
_ebay_token_lock = threading.Lock()

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
    for secret in (GOOGLE_API_KEY, NEWS_API_KEY, APP_SHARED_SECRET, EBAY_CLIENT_SECRET, _ebay_token.get("value")):
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
                    # OAuth style (eBay's token endpoint): {"error": "invalid_client", "error_description": "..."}
                    reason = error + (f": {body['error_description']}" if body.get("error_description") else "")
                errors = body.get("errors")  # eBay's APIs: {"errors": [{"message": ..., "longMessage": ...}]}
                if not reason and isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    reason = errors[0].get("longMessage") or errors[0].get("message") or ""
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


# ---------------------------------------------------------------------------------------------------- eBay


class _EbayAuthFailed(Exception):
    """eBay refused to issue a token (wrong keys, keyset not enabled...): `response` is eBay's reply, or None."""

    def __init__(self, response=None, exc=None):
        super().__init__("eBay token request failed")
        self.response = response
        self.exc = exc


def _ebay_base():
    env = (os.environ.get("EBAY_ENVIRONMENT") or "production").strip().lower()
    return EBAY_HOSTS.get(env, EBAY_HOSTS["production"])


def _ebay_access_token(force_new=False):
    """An application access token (client-credentials grant), reused until a minute before it expires."""
    with _ebay_token_lock:
        if not force_new and _ebay_token.get("value") and _ebay_token.get("expires_at", 0) - 60 > time.time():
            return _ebay_token["value"]
        basic = base64.b64encode(f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()).decode()
        try:
            resp = requests.post(
                f"{_ebay_base()}/identity/v1/oauth2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
                data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
                timeout=UPSTREAM_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise _EbayAuthFailed(exc=e) from None
        if not resp.ok:
            raise _EbayAuthFailed(response=resp)
        try:
            body = resp.json()
            token = body["access_token"]
            lifetime = int(body.get("expires_in") or 7200)
        except (ValueError, KeyError, TypeError):
            raise _EbayAuthFailed(exc=ValueError("token reply was not usable")) from None
        _ebay_token.update(value=token, expires_at=time.time() + lifetime)
        return token


def _ebay_get(path, params=None, marketplace=None):
    """GET an eBay Browse API path with the app token. A token eBay no longer accepts (401) is renewed once.
    Returns the requests.Response, or raises _EbayAuthFailed / requests.exceptions.RequestException."""
    for attempt in (1, 2):
        headers = {
            "Authorization": f"Bearer {_ebay_access_token(force_new=attempt == 2)}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace or os.environ.get("EBAY_MARKETPLACE_ID") or "EBAY_US",
            "Accept": "application/json",
        }
        campaign = (os.environ.get("EBAY_AFFILIATE_CAMPAIGN_ID") or "").strip()
        if campaign:
            headers["X-EBAY-C-ENDUSERCTX"] = f"affiliateCampaignId={campaign}"
        resp = requests.get(f"{_ebay_base()}{path}", params=params, headers=headers, timeout=UPSTREAM_TIMEOUT)
        if resp.status_code != 401:
            return resp
    return resp


def _ebay_failure(what, e):
    if isinstance(e, _EbayAuthFailed):
        return _upstream_error(f"{what} (eBay sign-in)", resp=e.response, exc=e.exc)
    return _upstream_error(what, exc=e)


def _bad_request(message):
    return jsonify({"error": {"message": message, "upstream_status": None}}), 400


def _price(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a number") from None
    if number < 0:
        raise ValueError(f"'{name}' must not be negative")
    return f"{number:g}"


@app.route("/shopping/ebay", methods=["GET"])
def shopping_ebay():
    _require_shared_secret()
    _require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")

    query = " ".join(request.args.get("q", "").split())
    if not query:
        abort(400, description="Missing required query parameter 'q'")
    if len(query) > 350:
        return _bad_request("Query parameter 'q' is too long (eBay allows 350 characters)")

    args = request.args
    params = {"q": query}
    try:
        params["limit"] = min(max(int(args.get("limit", 20)), 1), 200)
        params["offset"] = min(max(int(args.get("offset", 0)), 0), 9999)
    except ValueError:
        return _bad_request("'limit' and 'offset' must be whole numbers")

    sort = args.get("sort", "").strip()
    if sort:
        if sort not in EBAY_SORTS:
            return _bad_request(f"'sort' must be one of: {', '.join(sorted(EBAY_SORTS))}")
        params["sort"] = sort

    marketplace = args.get("marketplace", "").strip().upper() or None
    if marketplace and marketplace not in EBAY_MARKETPLACES:
        return _bad_request(f"Unknown 'marketplace' {marketplace!r}")

    filters = []
    try:
        low = _price(args["min_price"], "min_price") if args.get("min_price") else ""
        high = _price(args["max_price"], "max_price") if args.get("max_price") else ""
    except ValueError as e:
        return _bad_request(str(e))
    if low or high:
        filters.append(f"price:[{low}..{high}]")
    condition = args.get("condition", "").strip().lower()
    if condition:
        if condition not in ("new", "used"):
            return _bad_request("'condition' must be 'new' or 'used'")
        filters.append(f"conditions:{{{condition.upper()}}}")
    buying = args.get("buying", "").strip().lower()
    if buying:
        if buying not in ("fixed_price", "auction"):
            return _bad_request("'buying' must be 'fixed_price' or 'auction'")
        filters.append(f"buyingOptions:{{{buying.upper()}}}")
    if filters:
        params["filter"] = ",".join(filters)

    try:
        resp = _ebay_get("/buy/browse/v1/item_summary/search", params=params, marketplace=marketplace)
    except (_EbayAuthFailed, requests.exceptions.RequestException) as e:
        return _ebay_failure("eBay search request", e)
    if not resp.ok:
        return _upstream_error("eBay search request", resp=resp)
    try:
        return jsonify(resp.json())
    except ValueError as e:
        return _upstream_error("eBay search request", exc=e)


@app.route("/shopping/ebay/item/<path:item_id>", methods=["GET"])
def shopping_ebay_item(item_id):
    _require_shared_secret()
    _require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")

    if not re.fullmatch(r"v1\|\d{1,20}\|\d{1,20}", item_id):
        return _bad_request("item_id must look like v1|123456789|0 (the itemId from a search result)")
    marketplace = request.args.get("marketplace", "").strip().upper() or None
    if marketplace and marketplace not in EBAY_MARKETPLACES:
        return _bad_request(f"Unknown 'marketplace' {marketplace!r}")
    try:
        resp = _ebay_get(f"/buy/browse/v1/item/{requests.utils.quote(item_id, safe='')}", marketplace=marketplace)
    except (_EbayAuthFailed, requests.exceptions.RequestException) as e:
        return _ebay_failure("eBay item request", e)
    if not resp.ok:
        return _upstream_error("eBay item request", resp=resp)
    try:
        return jsonify(resp.json())
    except ValueError as e:
        return _upstream_error("eBay item request", exc=e)


if __name__ == "__main__":
    # Local dev only. On Render, gunicorn runs this via the Procfile/
    # start command instead (see README.md).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
