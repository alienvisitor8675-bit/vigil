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


def _drifted(path: Path) -> None:
    """A DB another branch migrated to user_version 8: steps 1-3 ran, #4's column is
    missing, and it has a table main has never heard of."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("ALTER TABLE model_calls DROP COLUMN truncated")
    conn.execute("CREATE TABLE mystery (name TEXT PRIMARY KEY, n INTEGER)")
    conn.execute("INSERT INTO mystery VALUES ('b', 3)")
    conn.execute(
        "INSERT INTO model_calls (purpose, provider, model, tokens_in, tokens_out, cost_usd,"
        " ts, provenance, tokens_cache_read, tokens_cache_write_5m, tokens_cache_write_1h)"
        " VALUES ('session','anthropic','m',1,2,0.5,'2026-10-20','claude',0,0,0)"
    )
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()


def test_every_step_runs_whatever_user_version_says(tmp_path: Path) -> None:
    path = tmp_path / "drift.db"
    _drifted(path)
    store = Store.open(path)
    assert "truncated" in _cols(store, "model_calls")
    row = store.q("SELECT * FROM model_calls")[0]
    assert row["cost_usd"] == 0.5 and row["truncated"] == 0
    assert store.q("SELECT n FROM mystery WHERE name='b'")[0]["n"] == 3
    assert store.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)


def test_running_migrate_twice_is_a_no_op(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "twice.db")
    store.x(
        "INSERT INTO inbox_items (queue, kind, title, body, created_at, provenance)"
        " VALUES ('specs','k','t','{}','now','human')"
    )
    before = [tuple(r) for r in store.q("SELECT * FROM sqlite_master ORDER BY name")]
    db.migrate(store.conn)
    db.migrate(store.conn)
    after = [tuple(r) for r in store.q("SELECT * FROM sqlite_master ORDER BY name")]
    assert before == after
    assert len(store.q("SELECT * FROM inbox_items")) == 1


def test_round2_adds_provenance_and_the_new_columns(tmp_path: Path) -> None:
    """Review B14: the four phase 2a tables carry provenance; owner rulings 1,
    4 and 6 add ladder_changes, model_calls.repo and decisions.kind."""
    path = tmp_path / "v.db"
    store = Store.open(path)
    for table in ("ladder", "intake_seen", "breakers", "jev_drift"):
        assert "provenance" in _cols(store, table), table
    assert "repo" in _cols(store, "model_calls") and "kind" in _cols(store, "decisions")
    assert {"shape", "old_rung", "new_rung", "reason", "sample_size", "match_rate",
            "provenance"} <= _cols(store, "ladder_changes")  # fmt: skip
    store.x(
        "INSERT INTO breakers (kind, scope, state, count, held, detail, updated_at)"
        " VALUES ('wip','repo:o/r','closed',0,'[]','{}','t')"
    )
    assert store.q("SELECT provenance FROM breakers")[0][0] == "human"
    store.conn.close()


def test_round2_migrates_an_m8_database_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    store = Store.open(path)
    store.conn.execute("PRAGMA user_version = 8")
    store.conn.close()
    again = Store.open(path)  # m9 runs again: idempotent
    assert again.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)


def _shape(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    return {
        t: sorted((r[1], r[2]) for r in conn.execute(f"PRAGMA table_info({t})").fetchall())
        for t in tables
    }


def test_two_a_steps_follow_mains_and_end_with_the_full_schema(tmp_path: Path) -> None:
    """A DB at main's steps (1-5) has none of 2a's tables or columns; 2a's steps,
    appended after, build them, tolerate tables an earlier dev run already made,
    and a second pass changes nothing."""
    assert [s.__name__ for s in db.MIGRATIONS[:5]] == [
        "_m1_gateway_token_binding",
        "_m2_dev_image_keys",
        "_m3_job_blocked_on",
        "_m4_call_truncated",
        "_m5_scrub_private_keys",
    ]
    fresh = Store.open(tmp_path / "fresh.db")  # SCHEMA, then every step
    want = _shape(fresh.conn)
    fresh.conn.close()

    path = tmp_path / "main.db"
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.executescript(db.SCHEMA)
    for t in ("ladder", "ladder_changes", "intake_seen", "transcripts", "breakers", "jev_drift"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")  # what main's steps leave: none of 2a's
    for table, col in (
        ("model_calls", "repo"),
        ("decisions", "kind"),
        ("gateway_tokens", "private"),
    ):
        if col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    for step in db.MIGRATIONS[:5]:  # main's steps, exactly as main has them
        conn.execute("BEGIN IMMEDIATE")
        step(conn)
        conn.execute("COMMIT")
    conn.execute("PRAGMA user_version = 5")
    conn.execute(  # a table a 2a dev run already made, with a row in it
        "CREATE TABLE IF NOT EXISTS jev_drift (route TEXT NOT NULL, version TEXT NOT NULL,"
        " pin TEXT NOT NULL, item_id INTEGER, first_seen TEXT NOT NULL,"
        " PRIMARY KEY (route, version))"
    )
    conn.execute("INSERT INTO jev_drift VALUES ('r','v','p',NULL,'t')")
    assert _shape(conn) != want
    db.migrate(conn)
    assert _shape(conn) == want
    assert conn.execute("SELECT route FROM jev_drift").fetchone()[0] == "r"
    before = [tuple(r) for r in conn.execute("SELECT * FROM sqlite_master ORDER BY name")]
    db.migrate(conn)
    assert before == [tuple(r) for r in conn.execute("SELECT * FROM sqlite_master ORDER BY name")]
    conn.close()
