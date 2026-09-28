"""The rules of developer access, against the real service (real scrypt, real Ed25519, fake clock).

Kept in step with the original Node licence server's tests: both implementations must behave the same, because the Mac app talks to either.
"""

from __future__ import annotations

import base64
import json
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.license import crypto as C
from app.license.service import MESSAGES
from tests.license_helpers import code_of, login_as, make_device, new_service, refresh_as

EXPIRED = "Sorry, your developer access mode has expired. Kindly reinstall the app from the Mac App Store or jonahbrowser.com, or please contact Customer Care Service."
DEVICE = "This account is already authorized on another device. Please contact Customer Care Service to transfer or reset your device authorization."


def with_account(**config):
    service, clock, key = new_service(**config)
    account = service.create_account("rohan_test", "Test-pass-1")
    return service, clock, key, account, make_device()


def verify(service, key, clock, token):
    return C.verify_token(token, {key.kid: key.public_key}, now=clock.t, issuer="jonah-license", audience="jonah-mac")


# ------------------------------------------------------------------ signing in

def test_a_first_login_gives_a_short_lived_signed_token_and_binds_the_device():
    service, clock, key, account, dev = with_account()
    r = login_as(service, dev, "rohan_test", "Test-pass-1")
    claims, why = verify(service, key, clock, r["accessToken"])
    assert why == "ok" and claims["usr"] == "rohan_test" and claims["unl"] == 1
    assert claims["exp"] - claims["iat"] == 180 and claims["nc"], "3 minutes, and bound to the request's challenge"
    view = service.get_account_view(account["id"])
    assert view["device"]["label"] == "Test Mac" and view["online"] is True


def test_passwords_are_stored_only_as_scrypt_hashes():
    service, *_ = with_account()
    rows = service.db.all("SELECT password_hash FROM accounts")
    assert rows[0]["password_hash"].startswith("scrypt$32768$8$1$")
    dump = json.dumps([dict(r) for r in service.db.all("SELECT * FROM accounts")]) + json.dumps(service.list_audit(500))
    assert "Test-pass-1" not in dump


def test_a_hash_made_by_the_node_server_verifies_here():
    # produced by license-server/src/crypto.cjs hashPassword("Interop-pass-1"): both implementations must accept each other's accounts
    node_hash = "scrypt$32768$8$1$1Dj7nxM0rWphcL6YkWPu0g$zggAWrGxeENUNSy4BzYkjtQtNWOSCA9Z60Cn9uqcot36sxsmZOvMucF4zUAjvRXohb97gNweQmzJmlslMnj0Kw"
    assert C.verify_password("Interop-pass-1", node_hash) is True
    assert C.verify_password("Interop-pass-2", node_hash) is False


def test_wrong_password_and_unknown_user_are_indistinguishable():
    service, _, _, _, dev = with_account()
    assert code_of(login_as, service, dev, "rohan_test", "nope-nope-1") == "invalid_credentials"
    assert code_of(login_as, service, dev, "nobody_here", "nope-nope-1") == "invalid_credentials"


def test_five_wrong_passwords_lock_that_user_and_ip_until_the_wait_passes():
    service, clock, _, _, dev = with_account()
    for i in range(5):
        assert code_of(login_as, service, dev, "rohan_test", f"wrong-pass-{i}") == "invalid_credentials"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "rate_limited"
    clock.t += 31
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "OK"


def test_a_guesser_on_another_ip_does_not_lock_the_real_user_out():
    service, _, _, _, dev = with_account()
    for i in range(6):
        code_of(login_as, service, make_device(), "rohan_test", f"guess-{i}", "66.6.6.6")
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1", "10.0.0.1") == "OK"


# ------------------------------------------------------------------ the master switches

def test_deactivating_the_whole_app_blocks_logins_and_running_sessions_and_reactivating_restores_them():
    service, _, _, _, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.update_settings({"appActive": False}, "admin:t")
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "access_expired"
    assert code_of(login_as, service, dev, "nobody", "whatever-1") == "access_expired", "nothing is learned about accounts while it is off"
    assert code_of(refresh_as, service, dev, s) == "access_expired"
    service.update_settings({"appActive": True}, "admin:t")
    assert code_of(refresh_as, service, dev, s) == "OK"


def test_turning_unlimited_mode_off_blocks_everyone_the_same_way():
    service, _, _, _, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.update_settings({"unlimitedEnabled": False}, "admin:t")
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "access_expired"
    assert code_of(refresh_as, service, dev, s) == "access_expired"


# ------------------------------------------------------------------ bans, disabling, expiry

def test_ban_takes_effect_at_the_next_check_for_logins_and_running_sessions_and_unban_restores():
    service, _, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "ban", "admin:t")
    assert code_of(refresh_as, service, dev, s) == "access_expired", "the expiry message, not 'session ended', even though banning revoked the session"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "access_expired"
    assert service.get_account_view(account["id"])["effectiveStatus"] == "banned"
    service.action(account["id"], "unban", "admin:t")
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "OK"


def test_disable_behaves_like_a_ban_but_shows_as_disabled():
    service, _, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "disable", "admin:t")
    assert code_of(refresh_as, service, dev, s) == "access_expired"
    assert service.get_account_view(account["id"])["effectiveStatus"] == "disabled"
    service.action(account["id"], "enable", "admin:t")
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "OK"


def test_an_expiry_date_ends_access_for_logins_and_running_sessions():
    service, clock, _, account, dev = with_account()
    service.update_account(account["id"], {"expiresAt": clock.t + 1000}, "admin:t")
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    clock.t += 600
    assert code_of(refresh_as, service, dev, s) == "OK"
    clock.t += 401
    assert code_of(refresh_as, service, dev, s) == "access_expired"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "access_expired"
    assert service.get_account_view(account["id"])["effectiveStatus"] == "expired"


# ------------------------------------------------------------------ one account, one device

def test_another_mac_with_the_right_password_is_refused_with_the_device_message():
    service, _, _, _, dev = with_account()
    login_as(service, dev, "rohan_test", "Test-pass-1")
    assert code_of(login_as, service, make_device("Other Mac"), "rohan_test", "Test-pass-1") == "device_conflict"
    assert MESSAGES["device_conflict"] == DEVICE
    assert MESSAGES["access_expired"] == EXPIRED
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "OK", "the original device still works"


def test_same_device_key_but_a_different_hardware_id_is_refused():
    service, _, _, _, dev = with_account()
    login_as(service, dev, "rohan_test", "Test-pass-1")
    clone = make_device()
    clone._key, clone.pub, clone.hw = dev._key, dev.pub, C.sha256_hex("another mac")
    assert code_of(login_as, service, clone, "rohan_test", "Test-pass-1") == "device_conflict"


def test_revoking_the_device_frees_the_account_and_ends_the_old_session():
    service, _, _, account, dev = with_account()
    first = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "revoke-device", "admin:t")
    assert service.get_account_view(account["id"])["device"] is None
    assert code_of(refresh_as, service, dev, first) == "session_ended"
    other = make_device("New Mac")
    assert code_of(login_as, service, other, "rohan_test", "Test-pass-1") == "OK"
    assert service.get_account_view(account["id"])["device"]["label"] == "New Mac"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "device_conflict"


def test_two_macs_racing_for_a_fresh_account_exactly_one_wins():
    service, *_ = with_account()
    results = []
    devices = [make_device("A"), make_device("B")]

    def go(d):
        results.append(code_of(login_as, service, d, "rohan_test", "Test-pass-1"))

    threads = [threading.Thread(target=go, args=(d,)) for d in devices]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["OK", "device_conflict"]


# ------------------------------------------------------------------ sessions

def test_force_reauthentication_ends_every_session_and_a_fresh_login_works():
    service, _, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "force-reauth", "admin:t")
    assert code_of(refresh_as, service, dev, s) == "session_ended"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "OK"


def test_a_refresh_needs_the_devices_signature_and_a_challenge_works_once():
    service, _, _, _, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    assert code_of(refresh_as, service, make_device("Thief"), s) == "bad_device_proof"
    ch = service.issue_challenge("10.0.0.1")
    args = dict(session_id=s["sessionId"], refresh_token=s["refreshToken"], challenge_id=ch["challengeId"], sig=dev.sign(f"refresh|{ch['nonce']}|{s['sessionId']}"), ip="10.0.0.1")
    assert code_of(service.refresh, **args) == "OK"
    assert code_of(service.refresh, **args) == "bad_challenge"


def test_a_stolen_refresh_token_alone_is_useless():
    service, clock, _, _, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    assert code_of(refresh_as, service, dev, {**s, "refreshToken": "x" * 43}) == "session_ended"
    clock.t += 16 * 60  # idle longer than 15 minutes
    assert code_of(refresh_as, service, dev, s) == "session_ended"
    s2 = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.logout(session_id=s2["sessionId"], refresh_token=s2["refreshToken"])
    assert code_of(refresh_as, service, dev, s2) == "session_ended"


def test_challenges_expire_after_60_seconds_and_unknown_ones_are_refused():
    service, clock, _, _, dev = with_account()
    ch = service.issue_challenge("10.0.0.1")
    clock.t += 61
    sig = dev.sign(f"login|{ch['nonce']}|rohan_test|{dev.hw}")
    device = {"pub": dev.pub, "hw": dev.hw, "label": "x", "sig": sig}
    assert code_of(service.login, username="rohan_test", password="Test-pass-1", challenge_id=ch["challengeId"], device=device, ip="10.0.0.1") == "bad_challenge"
    assert code_of(service.login, username="rohan_test", password="Test-pass-1", challenge_id="unknown", device=device, ip="10.0.0.1") == "bad_challenge"


# ------------------------------------------------------------------ account management

def test_changing_the_password_signs_the_user_out_and_the_old_password_stops_working():
    service, _, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.set_password(account["id"], "Brand-new-pass-2", "admin:t")
    assert code_of(refresh_as, service, dev, s) == "session_ended"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "invalid_credentials"
    assert code_of(login_as, service, dev, "rohan_test", "Brand-new-pass-2") == "OK"


def test_deleting_an_account_ends_its_sessions_and_removes_the_login():
    service, _, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.delete_account(account["id"], "admin:t")
    assert code_of(refresh_as, service, dev, s) == "session_ended"
    assert code_of(login_as, service, dev, "rohan_test", "Test-pass-1") == "invalid_credentials"


def test_editing_only_a_note_does_not_sign_the_user_out_but_changing_the_expiry_does():
    service, clock, _, account, dev = with_account()
    s = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.update_account(account["id"], {"note": "tester from Delhi"}, "admin:t")
    assert code_of(refresh_as, service, dev, s) == "OK"
    service.update_account(account["id"], {"expiresAt": clock.t + 99999}, "admin:t")
    assert code_of(refresh_as, service, dev, s) == "session_ended"


def test_usernames_are_case_insensitive_and_seeding_never_overwrites_an_existing_account():
    service, *_ = new_service()
    assert service.seed_accounts([{"username": "Rohan_One", "password": "Seed-pass-1"}, {"username": "Tomas_Two", "password": "Seed-pass-2"}]) == ["Rohan_One", "Tomas_Two"]
    assert service.seed_accounts([{"username": "rohan_one", "password": "DIFFERENT-9"}]) == []
    dev = make_device()
    assert code_of(login_as, service, dev, "ROHAN_ONE", "Seed-pass-1") == "OK"
    assert code_of(login_as, service, dev, "rohan_one", "DIFFERENT-9") == "invalid_credentials"


def test_the_account_list_shows_active_disabled_banned_expired_and_online():
    service, clock, _, _, _ = with_account()
    a, b, c, d = (service.create_account(name, "Pass-word-1") for name in ("acct_active", "acct_disabled", "acct_banned", "acct_expired"))
    service.action(b["id"], "disable", "t")
    service.action(c["id"], "ban", "t")
    service.update_account(d["id"], {"expiresAt": clock.t + 10}, "t")
    clock.t += 11
    login_as(service, make_device(), "acct_active", "Pass-word-1")
    o = service.overview()
    assert o["counts"] == {"active": 2, "disabled": 1, "banned": 1, "expired": 1, "online": 1}  # 2 active: acct_active + rohan_test
    assert next(x for x in o["accounts"] if x["id"] == a["id"])["online"] is True
    assert o["storage"]["persistent"] is True


def test_validation_refuses_bad_usernames_short_passwords_duplicates_and_bad_settings():
    service, *_ = new_service()
    assert code_of(service.create_account, "a b", "Long-enough-1") == "bad_request"
    assert code_of(service.create_account, "okname", "short") == "bad_request"
    service.create_account("okname", "Long-enough-1")
    assert code_of(service.create_account, "OKNAME", "Long-enough-1") == "bad_request"
    assert code_of(service.update_settings, {"appActive": "yes"}, "t") == "bad_request"
    assert code_of(service.update_settings, {"tokenTtlSeconds": 5}, "t") == "bad_request"
    assert code_of(service.action, 1, "explode", "t") == "bad_request"


def test_the_audit_log_records_actions_and_sign_in_outcomes_without_any_password():
    service, _, _, account, dev = with_account()
    code_of(login_as, service, dev, "rohan_test", "wrong-wrong-1")
    login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "ban", "admin:owner")
    log = service.list_audit(50)
    actions = {e["action"] for e in log}
    assert {"account_create", "login_failed", "device_bound", "login", "ban"} <= actions
    assert next(e for e in log if e["action"] == "ban")["actor"] == "admin:owner"
    assert "wrong-wrong-1" not in json.dumps(log) and "Test-pass-1" not in json.dumps(log)


# ------------------------------------------------------------------ tokens

def test_token_verification_rejects_tampering_the_wrong_algorithm_unknown_keys_audience_and_expiry():
    service, clock, key, _, dev = with_account()
    r = login_as(service, dev, "rohan_test", "Test-pass-1")
    h, p, s = r["accessToken"].split(".")
    keys = {key.kid: key.public_key}
    opts = dict(now=clock.t, issuer="jonah-license", audience="jonah-mac")
    claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert C.verify_token(r["accessToken"], keys, **opts)[1] == "ok"
    assert C.verify_token(f"{h}.{C.b64u(json.dumps({**claims, 'usr': 'someone_else'}).encode())}.{s}", keys, **opts)[1] == "bad_signature"
    assert C.verify_token(f"{C.b64u(json.dumps({'alg': 'none', 'typ': 'JLT', 'kid': key.kid}).encode())}.{p}.", keys, **opts)[1] == "bad_alg"
    assert C.verify_token(f"{C.b64u(json.dumps({'alg': 'HS256', 'typ': 'JLT', 'kid': key.kid}).encode())}.{p}.{s}", keys, **opts)[1] == "bad_alg"
    stranger = C.SigningKey(Ed25519PrivateKey.generate(), "kother", "x")
    assert C.verify_token(C.sign_token(claims, stranger), keys, **opts)[1] == "unknown_kid"
    assert C.verify_token(C.sign_token(claims, C.SigningKey(stranger.private_key, key.kid, "x")), keys, **opts)[1] == "bad_signature"
    assert C.verify_token(r["accessToken"], keys, **{**opts, "audience": "other-app"})[1] == "bad_audience"
    assert C.verify_token(r["accessToken"], keys, **{**opts, "now": clock.t + 180 + 31})[1] == "expired"
    assert C.verify_token("garbage", keys, **opts)[1] == "malformed"


def test_the_live_token_check_follows_the_account_immediately():
    """check_license_header is what lifts the rate limit: it must stop the moment the account, session or a master switch says no."""
    service, clock, key, account, dev = with_account()
    r = login_as(service, dev, "rohan_test", "Test-pass-1")
    assert service.check_license_header("Bearer " + r["accessToken"])["usr"] == "rohan_test"
    assert service.check_license_header(r["accessToken"]) is not None
    assert service.check_license_header("garbage") is None and service.check_license_header(None) is None and service.check_license_header("x" * 5000) is None

    service.update_settings({"appActive": False}, "t")
    assert service.check_license_header(r["accessToken"]) is None
    service.update_settings({"appActive": True, "unlimitedEnabled": False}, "t")
    assert service.check_license_header(r["accessToken"]) is None
    service.update_settings({"unlimitedEnabled": True}, "t")
    assert service.check_license_header(r["accessToken"]) is not None

    service.action(account["id"], "ban", "t")
    assert service.check_license_header(r["accessToken"]) is None, "a ban applies on the very next request, not when the token expires"
    service.action(account["id"], "unban", "t")
    assert service.check_license_header(r["accessToken"]) is None, "the ban also ended that session for good"

    r2 = login_as(service, dev, "rohan_test", "Test-pass-1")
    assert service.check_license_header(r2["accessToken"]) is not None
    service.action(account["id"], "force-reauth", "t")
    assert service.check_license_header(r2["accessToken"]) is None
    r3 = login_as(service, dev, "rohan_test", "Test-pass-1")
    service.action(account["id"], "revoke-device", "t")
    assert service.check_license_header(r3["accessToken"]) is None
    r4 = login_as(service, dev, "rohan_test", "Test-pass-1")
    clock.t += 181
    assert service.check_license_header(r4["accessToken"]) is None, "an expired token is not accepted"

    stranger_key = C.new_signing_key()
    forged = C.sign_token({"iss": "jonah-license", "aud": "jonah-mac", "sub": account["id"], "sid": r4["sessionId"], "did": "x", "unl": 1, "iat": clock.t, "exp": clock.t + 100}, stranger_key)
    assert service.check_license_header(forged) is None, "signed by another key"


# ------------------------------------------------------------------ administrators

def test_administrator_sign_in_lock_out_idle_expiry_and_password_change():
    service, clock, _ = new_service(admin_username="owner", admin_password="A-long-admin-pass-1")
    assert service.ensure_bootstrap_admin(lambda _t: None) == {"username": "owner", "generated": False}
    assert service.ensure_bootstrap_admin(lambda _t: None) is None, "only created once"
    for i in range(5):
        assert code_of(service.admin_login, username="owner", password=f"bad-pass-{i}", ip="9.9.9.9") == "invalid_credentials"
    assert code_of(service.admin_login, username="owner", password="A-long-admin-pass-1", ip="9.9.9.9") == "rate_limited"
    clock.t += 61
    s = service.admin_login(username="owner", password="A-long-admin-pass-1", ip="9.9.9.9")
    assert service.admin_from_token(s["token"])
    clock.t += 31 * 60
    assert service.admin_from_token(s["token"]) is None, "idle sessions expire"
    s2 = service.admin_login(username="owner", password="A-long-admin-pass-1", ip="9.9.9.9")
    me = service.admin_from_token(s2["token"])
    assert code_of(service.admin_change_password, me["adminId"], "wrong", "Another-long-pass-2", me["tokenHash"]) == "invalid_credentials"
    assert code_of(service.admin_change_password, me["adminId"], "A-long-admin-pass-1", "short", me["tokenHash"]) == "bad_request"
    service.admin_change_password(me["adminId"], "A-long-admin-pass-1", "Another-long-pass-2", me["tokenHash"])
    assert code_of(service.admin_login, username="owner", password="A-long-admin-pass-1", ip="8.8.8.8") == "invalid_credentials"
    assert code_of(service.admin_login, username="owner", password="Another-long-pass-2", ip="8.8.8.8") == "OK"


def test_the_administrator_password_can_be_reset_from_the_environment():
    service, *_ = new_service(admin_username="owner", admin_password="A-long-admin-pass-1")
    service.ensure_bootstrap_admin(lambda _t: None)
    service.config.admin_password, service.config.admin_reset_password = "Recovered-pass-999", True
    assert service.ensure_bootstrap_admin(lambda _t: None)["reset"] is True
    assert code_of(service.admin_login, username="owner", password="A-long-admin-pass-1", ip="1.1.1.1") == "invalid_credentials"
    assert code_of(service.admin_login, username="owner", password="Recovered-pass-999", ip="2.2.2.2") == "OK"


# ------------------------------------------------------------------ where the database may live

def test_storage_is_only_trusted_when_it_will_survive_a_restart(monkeypatch, tmp_path):
    from pathlib import Path

    from app.license import resolve_storage
    from tests.conftest import make_settings

    prod = dict(environment="production", license_data_dir=str(tmp_path / "lic"))
    # not on Render: the ordinary filesystem is durable
    monkeypatch.delenv("RENDER", raising=False)
    assert resolve_storage(make_settings(**prod)) == (tmp_path / "lic", True)
    # on Render WITHOUT a mounted disk the folder looks fine but is wiped on every restart: refused, or clearly temporary
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr("os.path.ismount", lambda p: str(p) == "/")
    assert resolve_storage(make_settings(**prod)) == (None, False)
    assert resolve_storage(make_settings(**prod, license_allow_temporary_storage=True)) == (tmp_path / "lic", False)
    # on Render WITH a disk mounted above the folder: trusted
    disk = str(tmp_path)
    monkeypatch.setattr("os.path.ismount", lambda p: str(p) == disk)
    assert resolve_storage(make_settings(**prod)) == (tmp_path / "lic", True)
    # nothing configured in production: off (or temporary if allowed); local development: ./data
    monkeypatch.delenv("RENDER", raising=False)
    assert resolve_storage(make_settings(environment="production"))[0] in (None, Path("/var/data/license"))
    assert resolve_storage(make_settings(environment="development")) == (Path("data") / "license", True)
