"""Developer access with SEVERAL worker processes sharing one database file (a host may start more than one).

Regression for a real failure: two or more workers starting together raced to create the tables / the administrator; the losers crashed with
"table settings already exists", so requests randomly got a 503 "Developer access is not available" depending on which worker answered.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time

import pytest

from app.license import crypto as C
from app.license.db import MIGRATIONS, Db
from app.license.service import AuthError, LicenseConfig, LicenseService
from tests.license_helpers import Clock, code_of, login_as, make_device

CHILD = r'''
import json, sys, time
sys.path[:] = json.loads(sys.argv[1])
target, folder = float(sys.argv[2]), sys.argv[3]
from app.license.db import Db
from app.license.service import LicenseService, LicenseConfig
from app.license import crypto as C
while time.time() < target:
    pass
try:
    svc = LicenseService(Db(folder + "/license.db"), LicenseConfig(admin_username="owner", admin_password="A-long-admin-pass-1"), C.new_signing_key())
    svc.ensure_bootstrap_admin(lambda t: None)
    svc.seed_accounts([{"username": "seeded_one", "password": "Seed-pass-1"}])
    svc.issue_challenge("1.1.1.1")
    print("OK")
except Exception as e:
    print("FAIL", type(e).__name__, str(e)[:120])
'''


def test_several_processes_starting_at_the_same_moment_all_start_cleanly(tmp_path):
    runs = 6
    target = time.time() + 5  # everyone waits for the same instant, then opens the brand-new database together
    procs = [
        subprocess.Popen([sys.executable, "-c", CHILD, json.dumps(sys.path), str(target), str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(runs)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append(out.strip() or err.strip()[-200:])
    assert results == ["OK"] * runs, results
    db = sqlite3.connect(str(tmp_path / "license.db"))
    assert db.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 1, "exactly one administrator, however many workers raced"
    assert db.execute("SELECT COUNT(*) FROM accounts WHERE username = 'seeded_one'").fetchone()[0] == 1
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_a_database_left_half_set_up_by_an_interrupted_start_is_repaired(tmp_path):
    path = tmp_path / "license.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")  # exactly the state the failing worker found
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()
    db = Db(path)  # used to raise: table settings already exists
    tables = {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"settings", "accounts", "sessions", "audit", "admins", "admin_sessions", "challenges"} <= tables
    assert db.one("PRAGMA user_version")[0] == len(MIGRATIONS)
    db.close()


def test_a_database_from_the_original_node_server_is_upgraded_in_place(tmp_path):
    path = tmp_path / "license.db"
    raw = sqlite3.connect(str(path))
    for statement in [s.strip() for s in MIGRATIONS[0].split(";") if s.strip()]:
        raw.execute(statement)
    raw.execute("PRAGMA user_version = 1")
    raw.execute("INSERT INTO accounts (username, password_hash, created_at, updated_at) VALUES ('kept', 'scrypt$x', 1, 1)")
    raw.commit()
    raw.close()
    db = Db(path)
    assert db.one("PRAGMA user_version")[0] == len(MIGRATIONS)
    assert db.one("SELECT username FROM accounts")["username"] == "kept", "existing data is untouched"
    assert db.all("SELECT * FROM challenges") == []
    db.close()


def two_workers(tmp_path):
    """Two service instances, each with its OWN connection to the same file: what two worker processes look like."""
    key = C.new_signing_key()
    clock = Clock()
    a = LicenseService(Db(tmp_path / "license.db"), LicenseConfig(), key, now=clock)
    b = LicenseService(Db(tmp_path / "license.db"), LicenseConfig(), key, now=clock)
    return a, b, clock


def test_a_sign_in_can_be_answered_by_a_different_worker_than_the_one_that_issued_its_challenge(tmp_path):
    a, b, _ = two_workers(tmp_path)
    a.create_account("rohan_test", "Test-pass-1")
    dev = make_device()
    ch = a.issue_challenge("10.0.0.1")  # worker A hands out the challenge ...
    sig = dev.sign(f"login|{ch['nonce']}|rohan_test|{dev.hw}")
    result = b.login(username="rohan_test", password="Test-pass-1", challenge_id=ch["challengeId"], device={"pub": dev.pub, "hw": dev.hw, "label": "x", "sig": sig}, ip="10.0.0.1")  # ... worker B accepts it
    assert result["ok"] is True
    # and the sessions/tokens it made work on either worker
    assert b.check_license_header(result["accessToken"]) is not None
    assert a.check_license_header(result["accessToken"]) is not None


def test_a_challenge_can_be_used_only_once_across_all_workers(tmp_path):
    a, b, clock = two_workers(tmp_path)
    ch = a.issue_challenge("10.0.0.1")
    assert a._consume_challenge(ch["challengeId"]) == ch["nonce"]
    assert b._consume_challenge(ch["challengeId"]) is None, "already used by worker A"
    old = b.issue_challenge("10.0.0.1")
    clock.t += 61
    assert a._consume_challenge(old["challengeId"]) is None, "expired"


def test_a_banned_account_is_refused_by_every_worker(tmp_path):
    a, b, _ = two_workers(tmp_path)
    account = a.create_account("rohan_test", "Test-pass-1")
    dev = make_device()
    session = login_as(b, dev, "rohan_test", "Test-pass-1")
    a.action(account["id"], "ban", "admin:t")  # banned through worker A ...
    assert b.check_license_header(session["accessToken"]) is None  # ... stops the unlimited access on worker B at once
    assert code_of(login_as, b, dev, "rohan_test", "Test-pass-1") == "access_expired"


def test_creating_the_same_account_on_two_workers_is_a_clean_error_not_a_crash(tmp_path):
    a, b, _ = two_workers(tmp_path)
    a.create_account("same_name", "Long-enough-1")
    with pytest.raises(AuthError) as caught:
        b.create_account("same_name", "Long-enough-1")
    assert caught.value.code == "bad_request" and "already exists" in caught.value.message


def test_the_administrator_is_created_once_however_many_workers_try(tmp_path):
    key = C.new_signing_key()
    cfg = LicenseConfig(admin_username="owner", admin_password="A-long-admin-pass-1")
    a = LicenseService(Db(tmp_path / "license.db"), cfg, key)
    b = LicenseService(Db(tmp_path / "license.db"), cfg, key)
    assert a.ensure_bootstrap_admin(lambda _t: None) == {"username": "owner", "generated": False}
    assert b.ensure_bootstrap_admin(lambda _t: None) is None
    assert b.db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 1
