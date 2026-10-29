"""Tiny SQLite layer (stdlib only). Swap for Postgres/SQLAlchemy when you outgrow it."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, created_at);
CREATE TABLE IF NOT EXISTS saved_replies (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS save_work (
    email TEXT NOT NULL, visitor TEXT NOT NULL, code TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    PRIMARY KEY (email, visitor)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
    path TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL
);
"""


def new_id() -> str:
    return uuid.uuid4().hex


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    # -- settings (JSON values) ---------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # -- messages ---------------------------------------------------------------
    def add_message(self, session_id: str, role: str, text: str) -> dict[str, Any]:
        msg = {"id": new_id(), "session_id": session_id, "role": role, "text": text,
               "created_at": time.time()}
        self.execute("INSERT INTO messages(id, session_id, role, text, created_at) VALUES(?,?,?,?,?)",
                     (msg["id"], session_id, role, text, msg["created_at"]))
        return msg
