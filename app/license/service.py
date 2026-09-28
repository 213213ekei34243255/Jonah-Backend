"""All developer-access decisions. The Mac app (and anything else) is never trusted with any of them."""

from __future__ import annotations

import re
import secrets as _secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from app.license import crypto as C
from app.license.db import Db
from app.license.throttle import FailureTracker, SlidingWindow

MESSAGES = {
    "access_expired": "Sorry, your developer access mode has expired. Kindly reinstall the app from the Mac App Store or jonahbrowser.com, or please contact Customer Care Service.",
    "device_conflict": "This account is already authorized on another device. Please contact Customer Care Service to transfer or reset your device authorization.",
    "invalid_credentials": "Incorrect username or password.",
    "session_ended": "Your session has ended. Please sign in again.",
    "rate_limited": "Too many attempts. Please wait a moment and try again.",
    "bad_request": "The request was not valid.",
    "bad_challenge": "The sign-in request expired. Please try again.",
    "bad_device_proof": "This device could not be verified. Please try again.",
}
STATUS = {"invalid_credentials": 401, "access_expired": 403, "device_conflict": 403, "session_ended": 401, "rate_limited": 429, "bad_request": 400, "bad_challenge": 400, "bad_device_proof": 400}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ACTIONS = {"ban", "unban", "disable", "enable", "revoke-device", "force-reauth", "kill-sessions"}


class AuthError(Exception):
    def __init__(self, code: str, message: str | None = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(message or MESSAGES.get(code, code))
        self.code = code
        self.message = message or MESSAGES.get(code, code)
        self.status = STATUS.get(code, 400)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class LicenseConfig:
    issuer: str = "jonah-license"
    audience: str = "jonah-mac"
    token_ttl_seconds: int = 180
    session_idle_seconds: int = 900
    max_sessions_per_account: int = 5
    admin_idle_seconds: int = 1800
    admin_max_seconds: int = 28800
    admin_username: str = ""
    admin_password: str = ""
    admin_reset_password: bool = False
    storage_persistent: bool = True  # False only when the operator chose temporary storage (the console then shows a warning)
    unlimited_rate_limit_per_minute: int = 0  # 0 = signed-in developer accounts are not rate-limited at all
    seed_accounts: list = field(default_factory=list)


class LicenseService:
    def __init__(self, db: Db, config: LicenseConfig, signing_key: C.SigningKey, now: Callable[[], int] | None = None) -> None:
        self.db, self.config, self.key = db, config, signing_key
        self.now = now or (lambda: int(time.time()))
        ms = lambda: self.now() * 1000  # noqa: E731
        self.pair_fails = FailureTracker(free=5, base_ms=30_000, max_ms=900_000, now_ms=ms)
        self.user_fails = FailureTracker(free=30, base_ms=60_000, max_ms=900_000, now_ms=ms)
        self.ip_fails = FailureTracker(free=20, base_ms=60_000, max_ms=900_000, now_ms=ms)
        self.admin_fails = FailureTracker(free=5, base_ms=60_000, max_ms=1_800_000, now_ms=ms)
        self.challenge_rate = SlidingWindow(60, 60_000, now_ms=ms)
        self.refresh_rate = SlidingWindow(120, 60_000, now_ms=ms)
        self._public_keys = {signing_key.kid: signing_key.public_key}
        with self.db.lock:
            for k, v in (("app_active", "1"), ("unlimited_enabled", "1"), ("token_ttl_seconds", str(config.token_ttl_seconds))):
                self.db.run("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", k, v)

    # ------------------------------------------------------------------ settings

    def get_settings(self) -> dict:
        rows = {r["key"]: r["value"] for r in self.db.all("SELECT key, value FROM settings")}
        try:
            ttl = int(rows.get("token_ttl_seconds") or self.config.token_ttl_seconds)
        except ValueError:
            ttl = self.config.token_ttl_seconds
        return {"appActive": rows.get("app_active") == "1", "unlimitedEnabled": rows.get("unlimited_enabled") == "1", "tokenTtlSeconds": ttl}

    def update_settings(self, patch: dict, actor: str, ip: str | None = None) -> dict:
        put = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        changed = []
        with self.db.lock:
            if "appActive" in patch:
                if not isinstance(patch["appActive"], bool):
                    raise AuthError("bad_request")
                self.db.run(put, "app_active", "1" if patch["appActive"] else "0")
                changed.append(f"appActive={str(patch['appActive']).lower()}")
            if "unlimitedEnabled" in patch:
                if not isinstance(patch["unlimitedEnabled"], bool):
                    raise AuthError("bad_request")
                self.db.run(put, "unlimited_enabled", "1" if patch["unlimitedEnabled"] else "0")
                changed.append(f"unlimitedEnabled={str(patch['unlimitedEnabled']).lower()}")
            if "tokenTtlSeconds" in patch:
                n = patch["tokenTtlSeconds"]
                if isinstance(n, bool) or not isinstance(n, int) or n < 60 or n > 3600:
                    raise AuthError("bad_request")
                self.db.run(put, "token_ttl_seconds", str(n))
                changed.append(f"tokenTtlSeconds={n}")
            if changed:
                self.audit(actor, "settings", None, ", ".join(changed), ip)
        return self.get_settings()

    def access_open(self) -> bool:
        s = self.get_settings()
        return s["appActive"] and s["unlimitedEnabled"]

    # ------------------------------------------------------------------ audit

    def audit(self, actor: str, action: str, target: str | None, detail: str | None, ip: str | None) -> None:
        with self.db.lock:
            self.db.run("INSERT INTO audit (ts, actor, action, target, detail, ip) VALUES (?,?,?,?,?,?)", self.now(), str(actor), str(action), None if target is None else str(target), None if detail is None else str(detail)[:300], ip or None)
            if _secrets.randbelow(50) == 0:
                self.db.run("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - 5000")

    def list_audit(self, limit=200) -> list[dict]:
        try:
            n = min(1000, max(1, int(limit)))
        except (TypeError, ValueError):
            n = 200
        return [dict(r) for r in self.db.all("SELECT id, ts, actor, action, target, detail, ip FROM audit ORDER BY id DESC LIMIT ?", n)]

    # ------------------------------------------------------------------ accounts

    def effective_status(self, a, now: int | None = None) -> str:
        now = self.now() if now is None else now
        if a["status"] == "banned":
            return "banned"
        if a["status"] == "disabled":
            return "disabled"
        if a["expires_at"] is not None and now >= a["expires_at"]:
            return "expired"
        return "active"

    def _by_name(self, username: str):
        return self.db.one("SELECT * FROM accounts WHERE username = ?", username)

    def _by_id(self, account_id):
        return self.db.one("SELECT * FROM accounts WHERE id = ?", account_id)

    @staticmethod
    def _validate_new_password(pw, minimum: int = 8) -> None:
        if not isinstance(pw, str) or len(pw) < minimum or len(pw) > C.MAX_PASSWORD_LENGTH:
            raise AuthError("bad_request", f"Password must be {minimum}-{C.MAX_PASSWORD_LENGTH} characters.")

    @staticmethod
    def _validate_username(u) -> None:
        if not isinstance(u, str) or not USERNAME_RE.match(u):
            raise AuthError("bad_request", "Username must be 3-32 characters: letters, digits, dot, dash or underscore.")

    @staticmethod
    def _parse_expiry(v):
        if v is None or v == "":
            return None
        try:
            n = int(v) if not isinstance(v, bool) else None
        except (TypeError, ValueError):
            n = None
        if n is None or n <= 0:
            raise AuthError("bad_request", "Expiry must be a date/time in the future or empty.")
        return n

    def create_account(self, username, password, expires_at=None, note="", *, actor: str = "system", ip: str | None = None) -> dict:
        self._validate_username(username)
        self._validate_new_password(password)
        expiry = self._parse_expiry(expires_at)
        if self._by_name(username):
            raise AuthError("bad_request", "That username already exists.")
        pw_hash = C.hash_password(password)
        t = self.now()
        with self.db.lock:
            if self._by_name(username):
                raise AuthError("bad_request", "That username already exists.")
            try:
                cur = self.db.run("INSERT INTO accounts (username, password_hash, expires_at, note, created_at, updated_at) VALUES (?,?,?,?,?,?)", username, pw_hash, expiry, str(note or "")[:200], t, t)
            except sqlite3.IntegrityError:  # another worker created it a moment ago
                raise AuthError("bad_request", "That username already exists.") from None
            self.audit(actor, "account_create", username, None, ip)
            return self.get_account_view(cur.lastrowid)

    def seed_accounts(self, accounts: list) -> list[str]:
        """Creates the accounts that do not exist yet and never touches an existing one."""
        created = []
        for a in accounts or []:
            if not isinstance(a, dict) or not a.get("username") or not a.get("password") or self._by_name(a["username"]):
                continue
            try:
                self.create_account(a["username"], a["password"], a.get("expiresAt"), a.get("note") or "seeded", actor="system")
            except AuthError:
                continue  # already there (perhaps just created by another worker)
            created.append(a["username"])
        return created

    def _view(self, a) -> dict:
        now = self.now()
        s = self.db.one("SELECT COUNT(*) AS n, MAX(last_used_at) AS last FROM sessions WHERE account_id = ? AND revoked = 0 AND expires_at > ?", a["id"], now)
        online = s["n"] > 0 and s["last"] is not None and s["last"] >= now - (self.get_settings()["tokenTtlSeconds"] + 60)
        return {
            "id": a["id"], "username": a["username"], "status": a["status"], "effectiveStatus": self.effective_status(a, now), "expiresAt": a["expires_at"], "note": a["note"],
            "createdAt": a["created_at"], "lastLoginAt": a["last_login_at"], "online": bool(online), "activeSessions": s["n"],
            "device": {"id": a["device_id"], "label": a["device_label"], "hw": (a["device_hw"] or "")[:12], "boundAt": a["device_bound_at"]} if a["device_pub"] else None,
        }  # fmt: skip

    def get_account_view(self, account_id):
        a = self._by_id(account_id)
        return self._view(a) if a else None

    def list_accounts(self) -> list[dict]:
        return [self._view(a) for a in self.db.all("SELECT * FROM accounts ORDER BY username COLLATE NOCASE")]

    def overview(self) -> dict:
        accounts = self.list_accounts()
        counts = {"active": 0, "disabled": 0, "banned": 0, "expired": 0, "online": 0}
        for a in accounts:
            counts[a["effectiveStatus"]] += 1
            if a["online"]:
                counts["online"] += 1
        return {"settings": self.get_settings(), "counts": counts, "accounts": accounts, "serverTime": self.now(), "storage": {"persistent": self.config.storage_persistent}}

    def update_account(self, account_id: int, patch: dict, actor: str, ip: str | None = None) -> dict:
        with self.db.lock:
            a = self._by_id(account_id)
            if not a:
                raise AuthError("bad_request", "No such account.")
            sets, vals, notes = [], [], []
            end_sessions = False
            if patch.get("username") is not None and patch["username"] != a["username"]:
                self._validate_username(patch["username"])
                other = self._by_name(patch["username"])
                if other and other["id"] != a["id"]:
                    raise AuthError("bad_request", "That username already exists.")
                sets.append("username = ?"); vals.append(patch["username"]); notes.append(f"username {a['username']} -> {patch['username']}"); end_sessions = True
            if "expiresAt" in patch:
                e = self._parse_expiry(patch["expiresAt"])
                sets.append("expires_at = ?"); vals.append(e); notes.append(f"expiresAt={e}"); end_sessions = True
            if patch.get("note") is not None:
                sets.append("note = ?"); vals.append(str(patch["note"])[:200]); notes.append("note")
            if sets:
                sets.append("updated_at = ?"); vals.append(self.now())
                try:
                    self.db.run(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", *vals, account_id)
                except sqlite3.IntegrityError:
                    raise AuthError("bad_request", "That username already exists.") from None
                # a rename or a new expiry date makes the signed-in user sign in again (a note edit does not)
                if end_sessions:
                    self._kill_sessions(account_id)
                self.audit(actor, "account_update", a["username"], ", ".join(notes), ip)
            return self.get_account_view(account_id)

    def set_password(self, account_id: int, password, actor: str, ip: str | None = None) -> dict:
        a = self._by_id(account_id)
        if not a:
            raise AuthError("bad_request", "No such account.")
        self._validate_new_password(password)
        pw_hash = C.hash_password(password)
        with self.db.lock:
            self.db.run("UPDATE accounts SET password_hash = ?, updated_at = ? WHERE id = ?", pw_hash, self.now(), account_id)
            self._kill_sessions(account_id)
            self.audit(actor, "password_change", a["username"], None, ip)
            return self.get_account_view(account_id)

    def delete_account(self, account_id: int, actor: str, ip: str | None = None) -> dict:
        with self.db.lock:
            a = self._by_id(account_id)
            if not a:
                raise AuthError("bad_request", "No such account.")
            self.db.run("DELETE FROM accounts WHERE id = ?", account_id)
            self.audit(actor, "account_delete", a["username"], None, ip)
        return {"deleted": True}

    def _kill_sessions(self, account_id: int) -> None:
        """Revokes every session and bumps the epoch: any refresh with an older session is refused from now on."""
        with self.db.lock:
            self.db.run("UPDATE accounts SET session_epoch = session_epoch + 1 WHERE id = ?", account_id)
            self.db.run("UPDATE sessions SET revoked = 1 WHERE account_id = ? AND revoked = 0", account_id)

    def action(self, account_id: int, action: str, actor: str, ip: str | None = None) -> dict:
        if action not in ACTIONS:
            raise AuthError("bad_request")
        with self.db.lock:
            a = self._by_id(account_id)
            if not a:
                raise AuthError("bad_request", "No such account.")
            t = self.now()

            def set_status(s: str) -> None:
                self.db.run("UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?", s, t, account_id)

            if action == "ban":
                set_status("banned"); self._kill_sessions(account_id)
            elif action == "unban":
                set_status("active")
            elif action == "disable":
                set_status("disabled"); self._kill_sessions(account_id)
            elif action == "enable":
                set_status("active")
            elif action == "revoke-device":
                self.db.run("UPDATE accounts SET device_pub = NULL, device_id = NULL, device_hw = NULL, device_label = NULL, device_bound_at = NULL, updated_at = ? WHERE id = ?", t, account_id)
                self._kill_sessions(account_id)
            else:  # force-reauth / kill-sessions
                self._kill_sessions(account_id)
            self.audit(actor, action, a["username"], None, ip)
            return self.get_account_view(account_id)

    # ------------------------------------------------------------------ challenges (single use, 60 s)

    def issue_challenge(self, ip: str) -> dict:
        if not self.challenge_rate.hit(f"c:{ip}"):
            raise AuthError("rate_limited", retry_after_seconds=30)
        t = self.now()
        with self.db.lock:
            if _secrets.randbelow(20) == 0:  # tidy up now and then
                self.db.run("DELETE FROM challenges WHERE exp < ?", t)
                if self.db.one("SELECT COUNT(*) AS n FROM challenges")["n"] > 20_000:
                    raise AuthError("rate_limited", retry_after_seconds=30)
            cid, nonce = C.random_token(16), C.random_token(32)
            self.db.run("INSERT INTO challenges (id, nonce, exp) VALUES (?,?,?)", cid, nonce, t + 60)
        return {"challengeId": cid, "nonce": nonce, "expiresIn": 60, "serverTime": t}

    def _consume_challenge(self, cid) -> str | None:
        """Single use, whichever worker asks: only the one whose DELETE removes the row gets the nonce."""
        with self.db.lock:
            row = self.db.one("SELECT nonce, exp FROM challenges WHERE id = ?", str(cid))
            if row is None:
                return None
            if self.db.run("DELETE FROM challenges WHERE id = ?", str(cid)).rowcount != 1:
                return None  # another worker used it first
        return row["nonce"] if row["exp"] >= self.now() else None

    # ------------------------------------------------------------------ client authentication

    def _token_for(self, account, session_id: str, epoch: int, device_id: str, nonce: str) -> dict:
        t, ttl = self.now(), self.get_settings()["tokenTtlSeconds"]
        claims = {"iss": self.config.issuer, "aud": self.config.audience, "sub": account["id"], "usr": account["username"], "did": device_id, "sid": session_id, "ep": epoch, "unl": 1, "nc": nonce, "iat": t, "exp": t + ttl}
        return {"accessToken": C.sign_token(claims, self.key), "expiresIn": ttl}

    @staticmethod
    def _validate_device(device) -> dict | None:
        if not isinstance(device, dict):
            return None
        parsed = C.parse_device_public_key(device.get("pub"))
        hw, sig = device.get("hw"), device.get("sig")
        if parsed is None or not isinstance(hw, str) or not re.fullmatch(r"[0-9a-f]{64}", hw) or not isinstance(sig, str) or len(sig) > 200:
            return None
        label = re.sub(r"[^\x20-\x7E]", "", str(device.get("label") or ""))[:80]
        return {"pub": device["pub"], "id": parsed[1], "hw": hw, "sig": sig, "label": label}

    def _lock_check(self, keys_trackers) -> None:
        locks = [ms for (locked, ms) in (t.check(k) for t, k in keys_trackers) if locked]
        if locks:
            raise AuthError("rate_limited", retry_after_seconds=-(-max(locks) // 1000))

    def login(self, *, username, password, challenge_id, device, ip: str) -> dict:
        now = self.now()
        lower = username.strip().lower() if isinstance(username, str) else ""
        pair_key, user_key, ip_key = f"{lower}|{ip}", lower, ip
        self._lock_check([(self.pair_fails, pair_key), (self.user_fails, user_key), (self.ip_fails, ip_key)])

        # Master switches: when the app or unlimited mode is off, nobody gets in, and nothing is learned about any account.
        if not self.access_open():
            self.audit("client", "login_denied", lower or "?", "access switched off", ip)
            raise AuthError("access_expired")

        nonce = self._consume_challenge(challenge_id)
        if not nonce:
            raise AuthError("bad_challenge")
        if not isinstance(password, str) or not password or len(password) > C.MAX_PASSWORD_LENGTH or not USERNAME_RE.match(lower):
            C.verify_against_dummy(password if isinstance(password, str) and password else "x")
            self._note_failure(pair_key, user_key, ip_key)
            self.audit("client", "login_failed", lower[:40] or "?", "malformed", ip)
            raise AuthError("invalid_credentials")
        account = self._by_name(lower)
        ok = C.verify_password(password, account["password_hash"]) if account else C.verify_against_dummy(password)
        if not ok:
            self._note_failure(pair_key, user_key, ip_key)
            self.audit("client", "login_failed", lower, "wrong password", ip)
            raise AuthError("invalid_credentials")
        self.pair_fails.success(pair_key)
        self.user_fails.success(user_key)

        eff = self.effective_status(account, now)
        if eff != "active":
            self.audit("client", "login_denied", account["username"], eff, ip)
            raise AuthError("access_expired")

        dev = self._validate_device(device)
        if not dev:
            raise AuthError("bad_request")
        if not C.verify_device_signature(dev["pub"], f"login|{nonce}|{lower}|{dev['hw']}", dev["sig"]):
            raise AuthError("bad_device_proof")

        with self.db.lock:
            if not account["device_pub"]:
                # first login: this Mac becomes the account's device. The conditional update makes two simultaneous first logins pick one winner.
                cur = self.db.run("UPDATE accounts SET device_pub = ?, device_id = ?, device_hw = ?, device_label = ?, device_bound_at = ?, updated_at = ? WHERE id = ? AND device_pub IS NULL", dev["pub"], dev["id"], dev["hw"], dev["label"], now, now, account["id"])
                if cur.rowcount != 1:
                    self.audit("client", "device_conflict", account["username"], "lost bind race", ip)
                    raise AuthError("device_conflict")
                self.audit("client", "device_bound", account["username"], dev["label"], ip)
            elif account["device_pub"] != dev["pub"] or account["device_hw"] != dev["hw"]:
                self.audit("client", "device_conflict", account["username"], dev["label"], ip)
                raise AuthError("device_conflict")

            fresh = self._by_id(account["id"])
            sid, refresh = C.random_hex(16), C.random_token(32)
            self.db.run("INSERT INTO sessions (id, account_id, refresh_hash, device_id, epoch, created_at, last_used_at, expires_at, ip) VALUES (?,?,?,?,?,?,?,?,?)",
                        sid, account["id"], C.sha256_hex(refresh), dev["id"], fresh["session_epoch"], now, now, now + self.config.session_idle_seconds, ip or None)
            # keep the newest few sessions; a crashed app must not pile them up
            self.db.run("UPDATE sessions SET revoked = 1 WHERE account_id = ? AND revoked = 0 AND id NOT IN (SELECT id FROM sessions WHERE account_id = ? AND revoked = 0 ORDER BY created_at DESC, rowid DESC LIMIT ?)", account["id"], account["id"], self.config.max_sessions_per_account)
            self.db.run("UPDATE accounts SET last_login_at = ? WHERE id = ?", now, account["id"])
            self.audit("client", "login", account["username"], dev["label"], ip)
        return {"ok": True, "sessionId": sid, "refreshToken": refresh, "username": account["username"], **self._token_for(account, sid, fresh["session_epoch"], dev["id"], nonce), "serverTime": now}

    def _note_failure(self, pair_key: str, user_key: str, ip_key: str) -> None:
        self.pair_fails.fail(pair_key)
        self.user_fails.fail(user_key)
        self.ip_fails.fail(ip_key)

    def refresh(self, *, session_id, refresh_token, challenge_id, sig, ip: str) -> dict:
        if not self.refresh_rate.hit(f"r:{ip}"):
            raise AuthError("rate_limited", retry_after_seconds=30)
        now = self.now()
        if not self.access_open():
            raise AuthError("access_expired")
        nonce = self._consume_challenge(challenge_id)
        if not nonce:
            raise AuthError("bad_challenge")
        if not isinstance(session_id, str) or not isinstance(refresh_token, str) or not isinstance(sig, str):
            raise AuthError("bad_request")

        s = self.db.one("SELECT * FROM sessions WHERE id = ?", session_id)
        # 1) prove the caller holds this session's secret; 2) only then say WHY access ended. A banned/disabled/expired account must be told
        # "access expired" even though banning also revoked its session, so the account status is checked before the session's own state.
        if not s or not C.safe_equal(C.sha256_hex(refresh_token), s["refresh_hash"]):
            raise AuthError("session_ended")
        account = self._by_id(s["account_id"])
        if not account:
            raise AuthError("session_ended")
        if self.effective_status(account, now) != "active":
            raise AuthError("access_expired")
        if s["revoked"] or s["expires_at"] < now:
            raise AuthError("session_ended")
        if account["session_epoch"] != s["epoch"]:
            raise AuthError("session_ended")
        if not account["device_pub"] or account["device_id"] != s["device_id"]:
            raise AuthError("session_ended")  # device authorization was revoked
        if not C.verify_device_signature(account["device_pub"], f"refresh|{nonce}|{session_id}", sig):
            raise AuthError("bad_device_proof")

        self.db.run("UPDATE sessions SET last_used_at = ?, expires_at = ? WHERE id = ?", now, now + self.config.session_idle_seconds, s["id"])
        return {"ok": True, **self._token_for(account, s["id"], s["epoch"], s["device_id"], nonce), "serverTime": now}

    def logout(self, *, session_id, refresh_token) -> dict:
        s = self.db.one("SELECT * FROM sessions WHERE id = ?", session_id) if isinstance(session_id, str) else None
        if s and isinstance(refresh_token, str) and C.safe_equal(C.sha256_hex(refresh_token), s["refresh_hash"]):
            self.db.run("UPDATE sessions SET revoked = 1 WHERE id = ?", s["id"])
        return {"ok": True}  # never reveals whether the session existed

    # ------------------------------------------------------------------ token checks for the rest of this backend

    def public_keys(self) -> dict:
        return {"issuer": self.config.issuer, "audience": self.config.audience, "keys": [{"kid": self.key.kid, "alg": "EdDSA", "spki": self.key.spki}]}

    def check_license_header(self, value: str | None) -> dict | None:
        """Claims of a developer token that is valid RIGHT NOW, or None. Beyond the signature and expiry it checks the live account, session
        and master switches, so banning an account or switching the app off stops its unlimited access on the very next request (not up to
        3 minutes later, when the token itself would expire)."""
        token = re.sub(r"^Bearer\s+", "", str(value or ""), flags=re.I).strip()
        if not token or len(token) > 2048:
            return None
        # skew=0: this server issued the token with its own clock, so there is no clock difference to forgive (other servers use a small skew)
        claims, _reason = C.verify_token(token, self._public_keys, now=self.now(), issuer=self.config.issuer, audience=self.config.audience, skew=0)
        if not claims or claims.get("unl") != 1 or not self.access_open():
            return None
        account = self._by_id(claims.get("sub"))
        if not account or self.effective_status(account) != "active" or not account["device_pub"] or account["device_id"] != claims.get("did"):
            return None
        s = self.db.one("SELECT revoked, expires_at, epoch FROM sessions WHERE id = ?", str(claims.get("sid")))
        if not s or s["revoked"] or s["expires_at"] < self.now() or s["epoch"] != account["session_epoch"]:
            return None
        return claims

    # ------------------------------------------------------------------ admins (the Developer Console)

    def ensure_bootstrap_admin(self, print_fn: Callable[[str], None] = print) -> dict | None:
        cfg = self.config
        existing = self.db.one("SELECT id FROM admins WHERE username = ?", cfg.admin_username) if cfg.admin_username else None
        if cfg.admin_reset_password and existing and cfg.admin_password:
            if len(cfg.admin_password) < 12:
                raise ValueError("LICENSE_ADMIN_PASSWORD must be at least 12 characters")
            self.db.run("UPDATE admins SET password_hash = ? WHERE id = ?", C.hash_password(cfg.admin_password), existing["id"])
            self.db.run("DELETE FROM admin_sessions WHERE admin_id = ?", existing["id"])
            self.audit("system", "admin_reset", cfg.admin_username, "password set from the environment", None)
            return {"username": cfg.admin_username, "generated": False, "reset": True}
        if self.db.one("SELECT COUNT(*) AS n FROM admins")["n"] > 0:
            return None
        username, password, generated = cfg.admin_username, cfg.admin_password, False
        if not username or not password:
            username, password, generated = username or "admin", C.random_token(15), True
        if len(password) < 12:
            raise ValueError("LICENSE_ADMIN_PASSWORD must be at least 12 characters")
        try:
            self.db.run("INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)", username, C.hash_password(password), self.now())
        except sqlite3.IntegrityError:
            return None  # another worker created the administrator a moment ago
        if generated:
            print_fn(f"\n  First administrator created for the Developer Console.\n  Username: {username}\n  Password: {password}\n  This is shown ONCE. Sign in and change it.\n")
        return {"username": username, "generated": generated}

    def admin_login(self, *, username, password, ip: str) -> dict:
        lower = username.strip().lower()[:64] if isinstance(username, str) else ""
        keys = [f"a:{lower}|{ip}", f"aip:{ip}"]
        self._lock_check([(self.admin_fails, k) for k in keys])
        admin = self.db.one("SELECT * FROM admins WHERE username = ?", lower) if lower else None
        ok = isinstance(password, str) and bool(password) and len(password) <= C.MAX_PASSWORD_LENGTH and (C.verify_password(password, admin["password_hash"]) if admin else C.verify_against_dummy(password))
        if not admin or not ok:
            for k in keys:
                self.admin_fails.fail(k)
            self.audit("admin?", "admin_login_failed", lower[:40], None, ip)
            raise AuthError("invalid_credentials")
        self.admin_fails.success(keys[0])
        token, csrf, t = C.random_token(32), C.random_token(24), self.now()
        with self.db.lock:
            self.db.run("INSERT INTO admin_sessions (token_hash, admin_id, csrf, created_at, last_used_at, ip) VALUES (?,?,?,?,?,?)", C.sha256_hex(token), admin["id"], csrf, t, t, ip or None)
            self.db.run("UPDATE admins SET last_login_at = ? WHERE id = ?", t, admin["id"])
            self.audit(f"admin:{admin['username']}", "admin_login", None, None, ip)
        return {"token": token, "csrf": csrf, "username": admin["username"]}

    def admin_from_token(self, token: str | None) -> dict | None:
        if not isinstance(token, str) or len(token) < 20:
            return None
        digest, t = C.sha256_hex(token), self.now()
        with self.db.lock:
            s = self.db.one("SELECT s.*, a.username FROM admin_sessions s JOIN admins a ON a.id = s.admin_id WHERE s.token_hash = ?", digest)
            if not s:
                return None
            if t - s["last_used_at"] > self.config.admin_idle_seconds or t - s["created_at"] > self.config.admin_max_seconds:
                self.db.run("DELETE FROM admin_sessions WHERE token_hash = ?", digest)
                return None
            self.db.run("UPDATE admin_sessions SET last_used_at = ? WHERE token_hash = ?", t, digest)
        return {"adminId": s["admin_id"], "username": s["username"], "csrf": s["csrf"], "tokenHash": digest}

    def admin_logout(self, token: str | None) -> None:
        if isinstance(token, str):
            self.db.run("DELETE FROM admin_sessions WHERE token_hash = ?", C.sha256_hex(token))

    def admin_change_password(self, admin_id: int, current, new, keep_token_hash: str = "") -> dict:
        admin = self.db.one("SELECT * FROM admins WHERE id = ?", admin_id)
        if not admin or not C.verify_password(str(current or ""), admin["password_hash"]):
            raise AuthError("invalid_credentials")
        if not isinstance(new, str) or len(new) < 12 or len(new) > C.MAX_PASSWORD_LENGTH:
            raise AuthError("bad_request", "Administrator password must be at least 12 characters.")
        pw_hash = C.hash_password(new)
        with self.db.lock:
            self.db.run("UPDATE admins SET password_hash = ? WHERE id = ?", pw_hash, admin_id)
            self.db.run("DELETE FROM admin_sessions WHERE admin_id = ? AND token_hash != ?", admin_id, keep_token_hash or "")
            self.audit(f"admin:{admin['username']}", "admin_password_change", None, None, None)
        return {"ok": True}
