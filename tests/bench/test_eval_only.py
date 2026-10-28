"""Bench 1a: `eval-only` provenance. The CHECK on every provenance column is
widened by an appended, idempotent migration; the exporter refuses the rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vigild import db
from vigild.db import Store
from vigild.jobs.model import Role
from vigild.provenance import Provenance, ProvenanceError, check_exportable, export_training_rows

PROV_TABLES = [
    "jobs", "tasks", "sessions", "events", "artifacts", "inbox_items", "approvals",
    "model_calls", "decisions", "audits", "transcripts", "ladder", "ladder_changes",
    "intake_seen", "breakers", "jev_drift",
]  # fmt: skip


def _old_database(path: Path) -> None:
    """A vigil.db made before this change: the CHECK lists three values."""
    old = db.SCHEMA.replace(",'eval-only'", "")
    assert "eval-only" not in old
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.execute(
        "INSERT INTO jobs VALUES ('o-0001','o/r','main','t','r','[]','human','spec_pending',"
        "NULL,NULL,0,NULL,'2026-10-20','2026-10-20','human')"
    )
    conn.commit()
    conn.close()


def test_the_value_exists_and_the_exporter_refuses_it() -> None:
    assert Provenance("eval-only") is Provenance.EVAL_ONLY
    row = {"id": 1, "provenance": "eval-only"}
    with pytest.raises(ProvenanceError):
        check_exportable(row)
    with pytest.raises(ProvenanceError):
        list(export_training_rows([{"id": 2, "provenance": "local"}, row]))


def test_the_exporter_excludes_a_bench_jobs_rows(store: Store) -> None:
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    store.add_artifact(job.id, "spec.md", "# s", Provenance.CLAUDE)
    store.add_event("x", {}, Provenance.HUMAN, job_id=job.id)
    rows = [dict(r) for r in store.q("SELECT * FROM artifacts")]
    assert {r["provenance"] for r in rows} == {"eval-only"}
    with pytest.raises(ProvenanceError):
        list(export_training_rows(rows))
    # a non-bench job's local rows still export
    other = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    store.add_artifact(other.id, "x", "y", Provenance.LOCAL)
    local = [dict(r) for r in store.q("SELECT * FROM artifacts WHERE job_id=?", (other.id,))]
    assert list(export_training_rows(local)) == local


def test_every_row_of_a_bench_job_carries_it(store: Store) -> None:
    job = store.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    task = store.create_task(job.id, {"task_id": f"{job.id}-w1"}, Provenance.CLAUDE)
    sess = store.create_session(job.id, Role.WORKER, task_id=task.id)
    store.add_event("e", {}, Provenance.HUMAN, job_id=job.id)
    store.add_event("e", {}, Provenance.CLAUDE, session_id=sess.id)  # found through the session
    store.add_artifact(job.id, "a", "b", Provenance.CLAUDE, task_id=task.id)
    store.create_inbox_item("questions", "page", job.id, "t", {})
    store.add_transcript(sess.id, "sdk", None, [{"x": 1}])
    from vigild.config import load
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    record_call(
        store, PriceTable(load().prices), session_id=sess.id, task_id=task.id, job_id=job.id,
        purpose="worker", provider="anthropic", backend_preset="claude-sonnet",
        model="claude-sonnet-5-5", usage=CallUsage(tokens_out=10),
    )  # fmt: skip
    for table in ("jobs", "tasks", "sessions", "events", "artifacts", "inbox_items",
                  "model_calls", "transcripts"):  # fmt: skip
        rows = store.q(f"SELECT provenance FROM {table}")  # noqa: S608
        assert rows and {r["provenance"] for r in rows} == {"eval-only"}, table


def test_the_migration_widens_an_old_database_and_runs_twice(tmp_path: Path) -> None:
    path = tmp_path / "vigil.db"
    _old_database(path)
    first = Store.open(path)  # migrates
    assert "eval-only" in first.q("SELECT sql FROM sqlite_schema WHERE name='jobs'")[0][0]
    first.conn.close()
    second = Store.open(path)  # and again: every step runs on every start
    for step in db.MIGRATIONS:  # and each step, once more, by hand
        second.conn.execute("BEGIN IMMEDIATE")
        step(second.conn)
        second.conn.execute("COMMIT")
    assert second.q("PRAGMA integrity_check")[0][0] == "ok"
    assert second.q("PRAGMA foreign_key_check") == []
    assert second.get_job("o-0001").provenance == "human"  # rows kept
    for table in PROV_TABLES:
        sql = second.q("SELECT sql FROM sqlite_schema WHERE name=?", (table,))[0][0]
        assert sql.count("'eval-only'") == 1, table
    # the widened CHECK accepts the value, and still refuses an unknown one
    job = second.create_job(
        repo="bench/fixture", base="main", title="t", request="r", notes=[], source="bench"
    )
    assert job.provenance == "eval-only"
    with pytest.raises(sqlite3.IntegrityError):
        second.x("INSERT INTO events (ts, type, data, provenance) VALUES ('t','x','{}','maybe')")


def test_a_current_database_migrates_cleanly_twice(tmp_path: Path) -> None:
    path = tmp_path / "now.db"
    Store.open(path).conn.close()
    again = Store.open(path)
    assert again.q("PRAGMA user_version")[0][0] == len(db.MIGRATIONS)
    assert again.q("PRAGMA integrity_check")[0][0] == "ok"
    db.migrate(again.conn)
    db.migrate(again.conn)
    assert again.q("PRAGMA integrity_check")[0][0] == "ok"
