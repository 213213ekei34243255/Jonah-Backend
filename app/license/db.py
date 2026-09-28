"""SQLite storage (Python's built-in sqlite3, no extra dependency). One file, one connection, one lock.

The schema is identical to the original Node licence server's, so its database file opens here unchanged.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE accounts (
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

    CREATE TABLE sessions (
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
    CREATE INDEX sessions_account ON sessions(account_id);

    CREATE TABLE audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT, ip TEXT
    );

    CREATE TABLE admins (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      last_login_at INTEGER
    );

    CREATE TABLE admin_sessions (
      token_hash TEXT PRIMARY KEY,
      admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
      csrf TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      last_used_at INTEGER NOT NULL,
      ip TEXT
    );
    """,
]


class Db:
    """A single shared connection guarded by one re-entrant lock, so the service's read-then-write sequences never interleave."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)  # autocommit; transactions are explicit
        self._conn.row_factory = sqlite3.Row
        with self.lock:
            self._conn.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000; PRAGMA synchronous = FULL;")
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current, len(MIGRATIONS)):
                self._conn.execute("BEGIN")
                try:
                    for statement in _statements(MIGRATIONS[version]):
                        self._conn.execute(statement)
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
