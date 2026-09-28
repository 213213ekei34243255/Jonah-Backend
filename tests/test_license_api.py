"""Developer access through the real app: the Mac app's API, the Developer Console API and page, HTTPS, and the two safety properties that
matter most for a server that also does search: licensing problems never stop search, and a developer token lifts the rate limit only
while the account is live."""

from __future__ import annotations

import json

import pytest

from app.license import crypto as C
from tests.conftest import FakeProvider, make_settings
from tests.license_helpers import make_device
from tests.test_search import web_results

pytestmark = pytest.mark.anyio

ADMIN, ADMIN_PASS = "owner", "A-long-admin-pass-1"
SEED = json.dumps([{"username": "rohan_test", "password": "Test-pass-1"}])


def licensed(tmp_path, **over):
    return make_settings(license_data_dir=str(tmp_path / "lic"), license_admin_username=ADMIN, license_admin_password=ADMIN_PASS, license_seed_accounts=SEED, **over)


async def client_login(client, dev, username="rohan_test", password="Test-pass-1"):
    ch = (await client.post("/v1/auth/challenge", json={})).json()
    sig = dev.sign(f"login|{ch['nonce']}|{username.lower()}|{dev.hw}")
    return await client.post("/v1/auth/login", json={"username": username, "password": password, "challengeId": ch["challengeId"], "device": {"pub": dev.pub, "hw": dev.hw, "label": dev.label, "sig": sig}})


async def admin_session(client):
    r = await client.post("/admin/api/login", json={"username": ADMIN, "password": ADMIN_PASS})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].split(";")[0]
    csrf = r.json()["csrf"]
    client.cookies.clear()

    async def call(method, path, body=None, **extra):
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf, **extra}
        return await client.request(method, "/admin/api" + path, headers=headers, json=body)

    return call, cookie, csrf, r


# ------------------------------------------------------------------ the Mac app's API

async def test_challenge_login_refresh_logout_over_http(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path))
    dev = make_device()
    r = await client_login(client, dev)
    assert r.status_code == 200
    s = r.json()
    assert s["accessToken"] and s["refreshToken"] and s["sessionId"] and "Test-pass-1" not in r.text

    ch = (await client.post("/v1/auth/challenge", json={})).json()
    ok = await client.post("/v1/session/refresh", json={"sessionId": s["sessionId"], "refreshToken": s["refreshToken"], "challengeId": ch["challengeId"], "sig": dev.sign(f"refresh|{ch['nonce']}|{s['sessionId']}")})
    assert ok.status_code == 200

    assert (await client.post("/v1/session/logout", json={"sessionId": s["sessionId"], "refreshToken": s["refreshToken"]})).status_code == 200
    ch2 = (await client.post("/v1/auth/challenge", json={})).json()
    after = await client.post("/v1/session/refresh", json={"sessionId": s["sessionId"], "refreshToken": s["refreshToken"], "challengeId": ch2["challengeId"], "sig": dev.sign(f"refresh|{ch2['nonce']}|{s['sessionId']}")})
    assert after.status_code == 401 and after.json()["code"] == "session_ended"


async def test_error_replies_carry_a_machine_code_and_the_right_status(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path))
    dev = make_device()
    bad = await client_login(client, dev, password="wrong-pass-1")
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
    await client_login(client, dev)
    other = await client_login(client, make_device())
    assert other.status_code == 403 and other.json()["code"] == "device_conflict"

    app.state.license.update_settings({"appActive": False}, "t")
    off = await client_login(client, dev)
    assert off.status_code == 403 and off.json()["code"] == "access_expired"
    app.state.license.update_settings({"appActive": True}, "t")

    for i in range(6):
        await client_login(client, make_device(), password=f"guess-{i}")
    locked = await client_login(client, make_device())
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0 and locked.json()["retryAfterSeconds"] > 0


async def test_bad_requests_are_refused_cleanly(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path))
    assert (await client.post("/v1/auth/nothing", json={})).status_code == 404
    assert (await client.get("/v1/auth/login")).status_code in (404, 405)
    broken = await client.post("/v1/auth/login", content=b"{not json", headers={"Content-Type": "application/json"})
    assert broken.status_code == 400 and broken.json()["code"] == "bad_request"
    big = await client.post("/v1/auth/login", json={"username": "x" * 40_000})
    assert big.status_code == 400
    assert (await client.get("/health")).status_code == 200


async def test_https_is_required_in_production_and_a_trusted_proxy_can_vouch_for_it(make_client, tmp_path):
    strict, _ = await make_client(licensed(tmp_path, environment="production"))
    r = await strict.post("/v1/auth/challenge", json={})
    assert r.status_code == 400 and r.json()["code"] == "https_required"
    assert (await strict.post("/v1/auth/challenge", json={}, headers={"X-Forwarded-Proto": "https"})).status_code == 400, "ignored unless a proxy is trusted"
    assert (await strict.get("/health")).status_code == 200
    assert (await strict.get("/admin/")).status_code == 400

    proxied, _ = await make_client(licensed(tmp_path / "p", environment="production", trust_proxy_headers=True))
    ok = await proxied.post("/v1/auth/challenge", json={}, headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})
    assert ok.status_code == 200
    assert "strict-transport-security" in ok.headers


async def test_public_keys_publish_only_the_public_half(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path))
    j = (await client.get("/v1/public-keys")).json()
    assert j["keys"][0]["kid"] == app.state.license.key.kid and j["keys"][0]["spki"] == app.state.license.key.spki
    assert "PRIVATE" not in json.dumps(j)


# ------------------------------------------------------------------ safety: licensing must never take search down

async def test_search_keeps_working_when_licensing_cannot_start(make_client, tmp_path):
    # production with no persistent disk and no override: licensing refuses to start (a wiped database would lock everyone out) ...
    settings = make_settings(environment="production", license_admin_username=ADMIN, license_admin_password=ADMIN_PASS, search_api_key="k" * 20)
    client, app = await make_client(settings, providers=[FakeProvider("a", web_results("a"))])
    assert app.state.license is None and "persistent" in app.state.license_problem
    r = await client.post("/v1/auth/challenge", json={}, headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 503 and r.json()["code"] == "not_configured"
    assert (await client.get("/admin/api/overview")).status_code in (400, 503)
    # ... and the search API is completely unaffected
    assert (await client.get("/health")).status_code == 200
    ok = await client.get("/search", params={"q": "python"}, headers={"Authorization": "Bearer " + "k" * 20})
    assert ok.status_code == 200 and ok.json()["results"]


async def test_licensing_can_be_switched_off_entirely(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path, license_enabled=False))
    assert app.state.license is None
    assert (await client.post("/v1/auth/challenge", json={})).status_code == 503


async def test_temporary_storage_needs_an_explicit_signing_key_and_shows_a_warning(make_client, tmp_path):
    no_key, app1 = await make_client(make_settings(environment="production", license_allow_temporary_storage=True, license_admin_username=ADMIN, license_admin_password=ADMIN_PASS))
    assert app1.state.license is None and "LICENSE_SIGNING_KEY" in app1.state.license_problem
    key = C.new_signing_key()
    pem = C.signing_key_pem(key)
    ok, app2 = await make_client(make_settings(environment="production", trust_proxy_headers=True, license_allow_temporary_storage=True, license_signing_key=pem, license_admin_username=ADMIN, license_admin_password=ADMIN_PASS))
    assert app2.state.license is not None and app2.state.license.key.kid == key.kid
    r = await ok.post("/admin/api/login", json={"username": ADMIN, "password": ADMIN_PASS}, headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    overview = await ok.get("/admin/api/overview", headers={"Cookie": r.headers["set-cookie"].split(";")[0], "X-Forwarded-Proto": "https"})
    assert overview.json()["storage"]["persistent"] is False


async def test_the_signing_key_survives_a_restart_and_a_second_start_reuses_the_same_database(make_client, tmp_path):
    settings = licensed(tmp_path)
    _, app1 = await make_client(settings)
    kid, spki = app1.state.license.key.kid, app1.state.license.key.spki
    _, app2 = await make_client(settings)
    assert (app2.state.license.key.kid, app2.state.license.key.spki) == (kid, spki), "a key that changed on restart would lock every Mac out"
    assert [a["username"] for a in app2.state.license.list_accounts()] == ["rohan_test"], "the seed is not applied twice"


# ------------------------------------------------------------------ the Developer Console

async def test_console_page_has_its_own_policy_while_the_api_keeps_the_strict_one(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path))
    page = await client.get("/admin/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.replace("style-src 'self'", "")
    assert "<script>" not in page.text.replace('<script src=', '')
    assert (await client.get("/admin/console.js")).status_code == 200 and (await client.get("/admin/console.css")).status_code == 200
    api = await client.get("/v1/public-keys")
    assert api.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    _, _, _, login = await admin_session(client)
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("samesite", "SameSite") or "samesite=strict" in cookie.lower()


async def test_console_api_needs_the_cookie_and_changes_need_the_csrf_token_and_a_same_site_origin(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path))
    assert (await client.get("/admin/api/overview")).status_code == 401
    call, cookie, csrf, _ = await admin_session(client)
    assert (await call("GET", "/overview")).status_code == 200
    assert (await client.patch("/admin/api/settings", json={"appActive": False}, headers={"Cookie": cookie})).status_code == 403
    assert (await call("PATCH", "/settings", {"appActive": False}, **{"X-CSRF-Token": "nope"})).status_code == 403
    assert (await call("PATCH", "/settings", {"appActive": False}, Origin="https://evil.example")).status_code == 403
    assert app.state.license.get_settings()["appActive"] is True, "none of the refused requests changed anything"
    assert (await call("PATCH", "/settings", {"appActive": False}, Origin="http://test")).status_code == 200
    assert app.state.license.get_settings()["appActive"] is False


async def test_every_management_action_from_the_brief_works_over_http(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path))
    call, *_ = await admin_session(client)
    dev = make_device()

    created = (await call("POST", "/accounts", {"username": "tomas_test", "password": "Other-pass-2", "note": "beta tester"})).json()
    account_id = created["account"]["id"]
    assert created["account"]["effectiveStatus"] == "active"
    assert (await client_login(client, dev, "tomas_test", "Other-pass-2")).status_code == 200
    overview = (await call("GET", "/overview")).json()
    row = next(a for a in overview["accounts"] if a["id"] == account_id)
    assert row["online"] is True and row["device"]["label"] == "Test Mac" and overview["counts"]["online"] == 1

    await call("POST", f"/accounts/{account_id}/action", {"action": "ban"})
    assert (await client_login(client, dev, "tomas_test", "Other-pass-2")).json()["code"] == "access_expired"
    await call("POST", f"/accounts/{account_id}/action", {"action": "unban"})
    assert (await client_login(client, dev, "tomas_test", "Other-pass-2")).status_code == 200

    assert (await client_login(client, make_device("Mac 2"), "tomas_test", "Other-pass-2")).json()["code"] == "device_conflict"
    await call("POST", f"/accounts/{account_id}/action", {"action": "revoke-device"})
    assert (await client_login(client, make_device("Mac 2"), "tomas_test", "Other-pass-2")).status_code == 200

    assert (await call("POST", f"/accounts/{account_id}/password", {"password": "Third-pass-3"})).status_code == 200
    assert (await call("PATCH", f"/accounts/{account_id}", {"expiresAt": 4_000_000_000, "note": "extended", "username": "tomas_renamed"})).status_code == 200
    assert (await call("POST", f"/accounts/{account_id}/action", {"action": "force-reauth"})).status_code == 200
    assert (await call("PATCH", "/settings", {"unlimitedEnabled": False})).json()["settings"]["unlimitedEnabled"] is False

    actions = {e["action"] for e in (await call("GET", "/audit?limit=100")).json()["entries"]}
    assert {"account_create", "ban", "unban", "revoke-device", "password_change", "account_update", "force-reauth", "settings"} <= actions
    assert (await call("DELETE", f"/accounts/{account_id}")).status_code == 200
    assert all(a["id"] != account_id for a in (await call("GET", "/overview")).json()["accounts"])


async def test_nothing_secret_is_ever_sent_to_the_browser(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path))
    call, *_ = await admin_session(client)
    await client_login(client, make_device())
    text = (await call("GET", "/overview")).text + (await call("GET", "/audit")).text
    for secret in ("scrypt$", "password_hash", "device_pub", "Test-pass-1", ADMIN_PASS, "PRIVATE KEY"):
        assert secret not in text, f"leaked {secret}"


async def test_the_console_can_be_limited_to_chosen_ips_or_switched_off(make_client, tmp_path):
    locked, _ = await make_client(licensed(tmp_path, license_admin_allowed_ips="203.0.113.50"))
    assert (await locked.get("/admin/")).status_code == 404
    assert (await locked.post("/admin/api/login", json={"username": ADMIN, "password": ADMIN_PASS})).status_code == 404
    assert (await locked.post("/v1/auth/challenge", json={})).status_code == 200, "the Mac app's API is unaffected"
    off, _ = await make_client(licensed(tmp_path / "o", license_admin_enabled=False))
    assert (await off.get("/admin/")).status_code == 404


async def test_console_sign_in_locks_after_repeated_failures(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path))
    for i in range(5):
        assert (await client.post("/admin/api/login", json={"username": ADMIN, "password": f"bad-password-{i}"})).status_code == 401
    assert (await client.post("/admin/api/login", json={"username": ADMIN, "password": ADMIN_PASS})).status_code == 429


async def test_no_password_ever_reaches_the_log(make_client, tmp_path, capsys, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    client, _ = await make_client(licensed(tmp_path))
    await client_login(client, make_device(), password="Test-pass-1")
    await client_login(client, make_device(), password="wrong-secret-pw-9")
    await client.post("/admin/api/login", json={"username": ADMIN, "password": "wrong-admin-pass-9"})
    text = caplog.text + capsys.readouterr().out
    for secret in ("Test-pass-1", "wrong-secret-pw-9", "wrong-admin-pass-9", ADMIN_PASS):
        assert secret not in text


# ------------------------------------------------------------------ "unlimited": the developer token lifts the rate limit, only while the account is live

async def test_a_signed_in_developer_is_not_rate_limited_but_everyone_else_still_is(make_client, tmp_path):
    settings = licensed(tmp_path, rate_limit_per_minute=3)
    client, app = await make_client(settings, providers=[FakeProvider("a", web_results("a"))])
    token = (await client_login(client, make_device())).json()["accessToken"]
    dev = {"X-Jonah-License": token}

    plain = [(await client.get("/search", params={"q": "python"})).status_code for _ in range(4)]
    assert plain == [200, 200, 200, 429], "without a token the normal limit applies"
    assert (await client.get("/search", params={"q": "python"}, headers=dev)).status_code == 200, "the developer is not held to it, even though this IP is over its limit"
    assert all([(await client.get("/search", params={"q": "python"}, headers=dev)).status_code == 200 for _ in range(10)])

    # a forged / garbage / someone-else's token gives nothing
    for bad in ("garbage", token[:-4] + "AAAA", "Bearer nonsense"):
        assert (await client.get("/search", params={"q": "python"}, headers={"X-Jonah-License": bad})).status_code == 429

    # ban: the very next request is back under the normal limit (no waiting for the token to expire)
    account_id = app.state.license.list_accounts()[0]["id"]
    app.state.license.action(account_id, "ban", "admin:t")
    assert (await client.get("/search", params={"q": "python"}, headers=dev)).status_code == 429


async def test_switching_the_app_or_unlimited_mode_off_ends_the_lifted_limit_at_once(make_client, tmp_path):
    client, app = await make_client(licensed(tmp_path, rate_limit_per_minute=1), providers=[FakeProvider("a", web_results("a"))])
    token = (await client_login(client, make_device())).json()["accessToken"]
    dev = {"X-Jonah-License": token}
    await client.get("/search", params={"q": "x"})  # uses this IP's single allowed request
    assert (await client.get("/search", params={"q": "x"}, headers=dev)).status_code == 200
    app.state.license.update_settings({"unlimitedEnabled": False}, "t")
    assert (await client.get("/search", params={"q": "x"}, headers=dev)).status_code == 429
    app.state.license.update_settings({"unlimitedEnabled": True, "appActive": False}, "t")
    assert (await client.get("/search", params={"q": "x"}, headers=dev)).status_code == 429
    app.state.license.update_settings({"appActive": True}, "t")
    assert (await client.get("/search", params={"q": "x"}, headers=dev)).status_code == 200


async def test_the_token_does_not_replace_the_api_key(make_client, tmp_path):
    key = "k" * 20
    client, _ = await make_client(licensed(tmp_path, search_api_key=key, rate_limit_per_minute=100), providers=[FakeProvider("a", web_results("a"))])
    token = (await client_login(client, make_device())).json()["accessToken"]
    assert (await client.get("/search", params={"q": "x"}, headers={"X-Jonah-License": token})).status_code == 401, "a licence token is not an API key"
    both = {"X-Jonah-License": token, "Authorization": f"Bearer {key}"}
    assert (await client.get("/search", params={"q": "x"}, headers=both)).status_code == 200


async def test_developer_accounts_can_be_given_their_own_limit_instead_of_none(make_client, tmp_path):
    client, _ = await make_client(licensed(tmp_path, rate_limit_per_minute=1, license_unlimited_rate_limit_per_minute=2), providers=[FakeProvider("a", web_results("a"))])
    token = (await client_login(client, make_device())).json()["accessToken"]
    dev = {"X-Jonah-License": token}
    codes = [(await client.get("/search", params={"q": "x"}, headers=dev)).status_code for _ in range(3)]
    assert codes == [200, 200, 429], "LICENSE_UNLIMITED_RATE_LIMIT_PER_MINUTE=2 applies to developer accounts instead of the normal 1"
    assert (await client.get("/search", params={"q": "x"})).status_code == 200, "and it is a separate allowance from the normal one"
