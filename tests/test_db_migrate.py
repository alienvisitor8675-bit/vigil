"""Schema migrations (review round 2, N8): a vigil.db made by an older vigild
opens, gains the new columns, and keeps its rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vigild import db
from vigild.db import Store

ROUND0_TOKENS = """CREATE TABLE IF NOT EXISTS gateway_tokens (
  token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL, repo TEXT NOT NULL,
  backend_preset TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);"""


def _round0(path: Path) -> None:
    start = db.SCHEMA.index("CREATE TABLE IF NOT EXISTS gateway_tokens")
    end = db.SCHEMA.index(";", start) + 1
    old = db.SCHEMA[:start] + ROUND0_TOKENS + db.SCHEMA[end:]
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.execute(
        "INSERT INTO gateway_tokens (token_hash, session_id, repo, backend_preset, created_at)"
        " VALUES ('h', 's-1', 'o/r', 'claude-haiku', '2026-10-20T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def _cols(store: Store, table: str) -> set[str]:
    return {r["name"] for r in store.q(f"PRAGMA table_info({table})")}


def test_a_round0_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "vigil.db"
    _round0(path)
    store = Store.open(path)
    assert {"model", "expires_at"} <= _cols(store, "gateway_tokens")
    row = store.q("SELECT * FROM gateway_tokens WHERE token_hash='h'")[0]
    assert row["session_id"] == "s-1" and row["model"] == "" and row["expires_at"] is None
    assert store.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)
    store.conn.close()
    again = Store.open(path)  # idempotent
    assert again.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)


def test_a_fresh_database_starts_at_the_latest_version(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "new.db")
    assert store.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)
    assert {"model", "expires_at"} <= _cols(store, "gateway_tokens")
