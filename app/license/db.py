"""SQLite storage (Python's built-in sqlite3, no extra dependency). One file, one connection, one lock.

The schema is identical to the original Node licence server's, so its database file opens here unchanged.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE IF NOT EXISTS accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','banned')),
      expires_at INTEGER,
      session_epoch INTEGER NOT NULL DEFAULT 0,
      device_pub TEXT, device_id TEXT, device_hw TEXT, device_label TEXT, device_bound_at INTEGER,
      note TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      last_login_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY,
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      refresh_hash TEXT NOT NULL,
      device_id TEXT NOT NULL,
      epoch INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      last_used_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,
      revoked INTEGER NOT NULL DEFAULT 0,
      ip TEXT
    );
    CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);

    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT, ip TEXT
    );

    CREATE TABLE IF NOT EXISTS admins (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      last_login_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS admin_sessions (
      token_hash TEXT PRIMARY KEY,
      admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
      csrf TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      last_used_at INTEGER NOT NULL,
      ip TEXT
    );
    """,
    # 2: the one-time sign-in challenges live in the shared database, so a request can be answered by a different worker than the one that issued it
    """
    CREATE TABLE IF NOT EXISTS challenges (
      id TEXT PRIMARY KEY,
      nonce TEXT NOT NULL,
      exp INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS challenges_exp ON challenges(exp);
    """,
]


class Db:
    """A single shared connection guarded by one re-entrant lock, so the service's read-then-write sequences never interleave.

    Several worker PROCESSES may open the same file at the same moment (a host can start more than one): every step that must happen exactly
    once (creating the tables) takes SQLite's write lock first and re-checks inside it, so the others simply wait and then find it done."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=15)  # autocommit; transactions are explicit; wait up to 15 s for another process's lock
        self._conn.row_factory = sqlite3.Row
        self._prepare()

    def _prepare(self) -> None:
        """Switch on WAL mode and bring the schema up to date. SQLite answers "database is locked" INSTANTLY (it does not wait) when several
        processes try to switch a brand-new file to WAL mode at the same moment, so this retries briefly instead of giving up: the process that
        loses simply tries again a fraction of a second later and finds the work done."""
        for attempt in range(60):
            try:
                with self.lock:
                    self._conn.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 15000; PRAGMA synchronous = FULL;")
                    self._migrate()
                return
            except sqlite3.OperationalError as exc:
                if not any(word in str(exc).lower() for word in ("locked", "busy")) or attempt == 59:
                    raise
                time.sleep(0.1 + random.random() * 0.3)

    def _version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        for version, script in enumerate(MIGRATIONS):
            if self._version() > version:
                continue
            self._conn.execute("BEGIN IMMEDIATE")  # the write lock: another process starting now waits here ...
            try:
                if self._version() <= version:  # ... and, once it has the lock, sees whether this step was already done
                    for statement in _statements(script):
                        self._conn.execute(statement)  # IF NOT EXISTS: also repairs a database left half-set-up by an interrupted start
                    self._conn.execute(f"PRAGMA user_version = {version + 1}")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def one(self, sql: str, *params):
        with self.lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, *params):
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def run(self, sql: str, *params) -> sqlite3.Cursor:
        with self.lock:
            return self._conn.execute(sql, params)

    def script(self, sql: str) -> None:
        with self.lock:
            self._conn.executescript(sql)

    def close(self) -> None:
        with self.lock:
            self._conn.close()


def _statements(script: str) -> list[str]:
    """Split a migration into single statements (executescript would COMMIT the surrounding transaction)."""
    return [s.strip() for s in script.split(";") if s.strip()]
