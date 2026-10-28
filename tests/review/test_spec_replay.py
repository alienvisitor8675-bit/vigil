"""Spec replay (ADR-0005 part 7): `POST /api/spec/replay` re-drafts a job's approved spec in
one coord session on the experiment line, `eval-only`, and reports the gate result, AC
coverage against the approved spec, the packet's size, the tokens and the cost. The session
is a fake: no hosted call runs here."""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.conftest import auth
from tests.jobs.test_coord_loop import SPEC, World, _approve, _request, plan
from tests.jobs.test_coord_loop import world as world
from tests.review.test_replay import ALL_TABLES, PROV_TABLES, _dump
from vigild import db
from vigild.breakers import GLOBAL, Kind
from vigild.executors.base import SessionSpec
from vigild.jobs.model import Role
from vigild.metering.calls import CallUsage, record_call
from vigild.metering.prices import PriceTable
from vigild.packets.assemble import COORD_PACKETS, assemble_coord
from vigild.repos import set_visibility_source
from vigild.review.replay import ac_coverage

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
from vigil.main import app

AC2_CMD = "pytest -q tests/test_runbook.py"


def _post(w: World, job: str, packet: str = "classic", **extra: Any) -> Any:
    return w.client.post(
        "/api/spec/replay",
        headers=auth(w.tokens.admin),
        json={"job_id": job, "packet": packet, **extra},
    )


def _approved(w: World) -> str:
    job = _approve(w)
    assert w.store.get_artifact(job, "approval.json") is not None
    return job


def _spend(w: World, **usage: int) -> Any:
    """A coord session that books tokens through the meter."""
    prices = PriceTable(w.runner.cfg.prices)

    def run(spec: SessionSpec) -> None:
        if spec.role is Role.COORD and w.store.is_replay(spec.session_id):
            record_call(
                w.store, prices, session_id=spec.session_id, task_id=None, job_id=None,
                purpose="session", provider="anthropic", backend_preset="claude-opus",
                model="claude-opus-5-5", usage=CallUsage(**usage),
            )  # fmt: skip

    return run


def _replay_rows(w: World) -> list[Any]:
    return w.store.q("SELECT * FROM replays WHERE kind='spec'")


def _sessions(w: World, since: int) -> list[SessionSpec]:
    return w.standin.specs[since:]


# --- the rule ------------------------------------------------------------------------


def test_the_coverage_rule_is_same_id_and_same_command_after_whitespace() -> None:
    approved = SPEC
    same = SPEC.replace(
        "pytest -q tests/test_feature_map.py", "pytest   -q  tests/test_feature_map.py"
    )
    assert ac_coverage(approved, same) == {"approved": 2, "covered": 2, "missing": [], "ok": True}
    other_cmd = SPEC.replace(AC2_CMD, "pytest -q tests/test_other.py")
    got = ac_coverage(approved, other_cmd)
    assert got["missing"] == ["AC2"] and got["covered"] == 1 and got["ok"] is False
    renumbered = SPEC.replace("AC2:", "AC9:")
    assert ac_coverage(approved, renumbered)["missing"] == ["AC2"]
    no_cmd = SPEC.replace(f" test: `{AC2_CMD}`", "")
    assert ac_coverage(approved, no_cmd)["missing"] == ["AC2"]
    extra = SPEC.replace("## Non-goals", "- AC3: more. test: `pytest -q x`\n\n## Non-goals")
    assert ac_coverage(approved, SPEC)["ok"] and ac_coverage(extra, SPEC)["missing"] == []


def test_the_variants_are_registered_in_one_dict() -> None:
    assert COORD_PACKETS["classic"] is assemble_coord


# --- the route -----------------------------------------------------------------------


def test_admin_only(world: World) -> None:
    w = world
    job = _approved(w)
    body = {"job_id": job, "packet": "classic"}
    assert w.client.post("/api/spec/replay", json=body).status_code == 401
    for token in (w.tokens.read, w.tokens.heartbeat):
        r = w.client.post("/api/spec/replay", headers=auth(token), json=body)
        assert r.status_code == 403
    assert _replay_rows(w) == []


@pytest.mark.parametrize(
    "body",
    [
        {"packet": "classic"},
        {"job_id": "x"},
        {"job_id": "x", "packet": ""},
        {"job_id": "x", "packet": "classic", "visibility": "public"},
        {"job_id": 7, "packet": "classic"},
    ],
)
def test_the_body_is_validated(world: World, body: dict[str, Any]) -> None:
    r = world.client.post("/api/spec/replay", headers=auth(world.tokens.admin), json=body)
    assert r.status_code == 422, r.text


def test_an_unknown_variant_is_422_and_nothing_runs(world: World) -> None:
    w = world
    job = _approved(w)
    before = len(w.standin.specs)
    r = _post(w, job, "no-such-variant")
    assert r.status_code == 422 and "no-such-variant" in r.text
    assert len(w.standin.specs) == before and _replay_rows(w) == []


def test_unknown_or_unapproved_jobs_are_refused(world: World) -> None:
    w = world
    assert _post(w, "nope-0001").status_code == 404
    job = _request(w)  # drafted and gated, never approved
    before = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 422 and "approved" in r.text
    assert len(w.standin.specs) == before and _replay_rows(w) == []


def test_it_returns_and_stores_the_result(world: World) -> None:
    w = world
    job = _approved(w)
    w.standin.on_session = _spend(
        w, tokens_in=1000, tokens_out=2000, tokens_cache_read=30000,
        tokens_cache_write_5m=4000, tokens_cache_write_1h=500,
    )  # fmt: skip
    since = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 200, r.text
    out = r.json()
    (spec,) = _sessions(w, since)
    assert spec.role is Role.COORD
    assert out["id"].startswith("rp-") and out["status"] == "done"
    assert out["kind"] == "spec" and out["job_id"] == job and out["packet"] == "classic"
    assert out["provenance"] == "eval-only"
    assert out["gate"] == {"ok": True, "errors": []}
    assert out["ac_coverage"] == {"approved": 2, "covered": 2, "missing": [], "ok": True}
    assert out["packet_chars"] == len(spec.packet)
    assert out["tokens"] == {"in": 1000, "cache_read": 30000, "cache_write_5m": 4000,
                             "cache_write_1h": 500, "out": 2000}  # fmt: skip
    (sid,) = {str(x["session_id"]) for x in _replay_rows(w)}
    cost = float(
        w.store.q("SELECT SUM(cost_usd) c FROM model_calls WHERE session_id=?", (sid,))[0]["c"]
    )
    assert cost > 0 and out["cost_usd"] == pytest.approx(cost)
    (row,) = _replay_rows(w)
    assert json.loads(row["result"])["ac_coverage"]["ok"] is True
    assert row["provenance"] == "eval-only" and row["job_id"] == job


def test_it_drafts_from_the_original_request_and_notes_at_the_recorded_base(world: World) -> None:
    w = world
    job = _approved(w)
    j = w.store.get_job(job)
    (first,) = [s for s in w.standin.specs if s.role is Role.COORD]
    ev = w.store.q("SELECT data FROM events WHERE job_id=? AND type='packet' ORDER BY id", (job,))
    base = json.loads(ev[0]["data"])["base_sha"]
    assert len(base) == 40 and f'"base_sha": "{base}"' in first.packet
    since = len(w.standin.specs)
    assert _post(w, job).status_code == 200
    (spec,) = _sessions(w, since)
    assert f'"base_sha": "{base}"' in spec.packet
    assert j.request in spec.packet and "keep it short" in spec.packet
    assert "gate refused" not in spec.packet and "previous_" not in spec.packet


def test_a_job_from_before_the_event_carried_a_base_uses_the_clone_record(world: World) -> None:
    w = world
    job = _approved(w)
    w.store.x("UPDATE events SET data=json_remove(data, '$.base_sha') WHERE type='packet'")
    since = len(w.standin.specs)
    assert _post(w, job).status_code == 200
    assert len(_sessions(w, since)) == 1


# --- money and effects ---------------------------------------------------------------


def test_it_books_only_to_the_experiment_line(world: World) -> None:
    w = world
    job = _approved(w)
    w.standin.on_session = _spend(w, tokens_out=10000)
    day, month, exp = (
        w.budget.spent_today(),
        w.budget.spent_month(),
        w.budget.spent_experiment_month(),
    )
    assert _post(w, job).status_code == 200
    assert w.budget.spent_today() == pytest.approx(day) and w.budget.open_today() == 0
    assert w.budget.spent_experiment_month() > exp
    assert w.budget.spent_month() > month
    (sid,) = {str(x["session_id"]) for x in _replay_rows(w)}
    res = w.store.q("SELECT pool, status FROM reservations WHERE session_id=?", (sid,))
    assert [(x["pool"], x["status"]) for x in res] == [("experiment", "settled")]
    assert {x["pool"] for x in w.store.q("SELECT pool FROM ledger WHERE session_id=?", (sid,))} == {
        "experiment"
    }
    s = w.store.get_session(sid)
    assert s.job_id is None and s.role is Role.COORD and s.provenance == "eval-only"


def test_every_row_is_eval_only_and_it_publishes_and_files_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job = _approved(w)
    w.standin.on_session = _spend(w, tokens_out=10000)
    before = _dump(w.store)
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()
    prs, closed = list(w.publisher.prs), list(w.publisher.closed)
    tree = w.client.get(f"/api/jobs/{job}/tree", headers=auth(w.tokens.read)).json()

    def no_network(self: socket.socket, address: Any) -> None:
        raise AssertionError(f"a spec replay opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert _post(w, job).status_code == 200
    after = _dump(w.store)
    for table, old in before.items():
        assert after[table][: len(old)] == old, f"{table}: an existing row changed"
        new = after[table][len(old) :]
        if table in PROV_TABLES:
            assert {x["provenance"] for x in new} <= {"eval-only"}, table
        if table in ("reservations", "ledger"):
            assert {x["pool"] for x in new} <= {"experiment"}, table
    for table in ("jobs", "tasks", "artifacts", "inbox_items", "approvals", "ladder", "audits"):
        assert after[table] == before[table], table
    assert {t for t in ALL_TABLES if after[t] != before[t]} >= {"sessions", "model_calls"}
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json() == inbox
    assert w.client.get(f"/api/jobs/{job}/tree", headers=auth(w.tokens.read)).json() == tree
    assert (w.publisher.prs, w.publisher.closed) == (prs, closed)
    assert w.store.get_job(job).state.value == "ready"


def test_the_gate_runs_once_with_no_redraft(world: World) -> None:
    w = world
    job = _approved(w)
    bad = SPEC.replace(f" test: `{AC2_CMD}`", "")
    w.standin.coord_drafts = [lambda j: (bad, plan(j))] * 3
    since = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 200, r.text
    assert len(_sessions(w, since)) == 1  # not three drafts
    out = r.json()
    assert out["status"] == "done" and out["gate"]["ok"] is False
    assert any("AC2" in e for e in out["gate"]["errors"])
    assert out["ac_coverage"]["missing"] == ["AC2"]
    assert [
        x["type"]
        for x in w.store.q("SELECT type FROM events WHERE type='spec_gate' AND job_id=?", (job,))
    ] == []
    assert w.store.get_artifact(job, "spec_gate.json") is not None  # the original's, untouched


def test_coverage_against_the_approved_spec_missing_and_present(world: World) -> None:
    w = world
    job = _approved(w)
    changed = SPEC.replace(AC2_CMD, "pytest -q tests/test_other.py")
    w.standin.coord_drafts = [lambda j: (changed, plan(j))]
    out = _post(w, job).json()
    assert out["ac_coverage"] == {"approved": 2, "covered": 1, "missing": ["AC2"], "ok": False}
    out2 = _post(w, job).json()  # the default draft is the approved spec itself
    assert out2["ac_coverage"]["ok"] is True and out2["ac_coverage"]["missing"] == []


def test_a_failed_session_is_reported_and_files_nothing(world: World) -> None:
    w = world
    job = _approved(w)
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()

    def boom(spec: SessionSpec) -> None:
        if w.store.is_replay(spec.session_id):
            raise RuntimeError("container died")

    w.standin.on_session = boom
    r = _post(w, job)
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["gate"] is None
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json() == inbox
    assert w.budget.open_experiment() == 0.0


def test_any_open_breaker_refuses(world: World) -> None:
    w = world
    job = _approved(w)
    w.runner.breakers.trip(Kind.DAILY, GLOBAL, {"why": "test"})
    before = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 409 and "breaker" in r.text
    assert len(w.standin.specs) == before and _replay_rows(w) == []


def test_a_full_experiment_line_refuses(world: World) -> None:
    w = world
    job = _approved(w)
    w.runner.cfg.budgets.experiment_monthly_usd = 0.01
    before = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 409 and "experiment" in r.text
    assert len(w.standin.specs) == before
    (row,) = _replay_rows(w)
    assert (row["status"], row["provenance"]) == ("refused", "eval-only")


def test_a_private_repo_never_reaches_a_backend_the_rule_forbids(world: World) -> None:
    w = world
    job = _approved(w)
    set_visibility_source(lambda repo: True)
    before = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 403 and "private" in r.text
    assert len(w.standin.specs) == before and w.budget.open_experiment() == 0.0
    set_visibility_source(lambda repo: False)
    assert _post(w, job).status_code == 200


def test_a_private_repo_is_refused_at_the_session_too(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job = _approved(w)
    set_visibility_source(lambda repo: True)
    monkeypatch.setattr("vigild.jobs.runner.repo_rule_errors", lambda cfg, repo, private: [])
    before = len(w.standin.specs)
    r = _post(w, job)
    assert r.status_code == 403
    assert len(w.standin.specs) == before and w.budget.open_experiment() == 0.0
    (row,) = _replay_rows(w)
    assert row["status"] == "refused"


# --- the migration -------------------------------------------------------------------


def test_the_replay_kind_step_is_last_and_idempotent(tmp_path: Path) -> None:
    assert db.MIGRATIONS[-1].__name__ == "_m17_replay_kind"
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("DROP TABLE IF EXISTS replays")
    conn.execute(
        db.REPLAY_DDL.replace(" kind TEXT NOT NULL DEFAULT 'review',", "").replace(
            " result TEXT NOT NULL DEFAULT '{}',", ""
        )
    )
    conn.execute(
        "INSERT INTO replays (id, job_id, task_id, head, prompt_version, prompt_sha256, status,"
        " created_at, provenance) VALUES ('rp-1','j','t','h','v','s','done','now','eval-only')"
    )
    conn.commit()
    conn.close()
    store = db.Store.open(path)
    cols = {r[1] for r in store.q("PRAGMA table_info(replays)")}
    assert {"kind", "result"} <= cols
    db.migrate(store.conn)
    db.migrate(store.conn)
    (row,) = store.q("SELECT kind, result FROM replays")
    assert (row["kind"], row["result"]) == ("review", "{}")


# --- the CLI -------------------------------------------------------------------------


def test_the_cli_is_a_client_of_the_api_and_prints_the_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    from vigild import config

    seen: dict[str, Any] = {}
    result = {
        "id": "rp-1", "job_id": "vi-0002", "packet": "classic", "status": "done",
        "gate": {"ok": False, "errors": ["spec.md AC2 has no test command"]},
        "ac_coverage": {"approved": 2, "covered": 1, "missing": ["AC2"], "ok": False},
        "packet_chars": 41234, "cost_usd": 0.4321,
        "tokens": {"in": 10, "cache_read": 20, "cache_write_5m": 30, "cache_write_1h": 40,
                   "out": 50},
    }  # fmt: skip

    class Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return result

    def post(url: str, **kw: Any) -> Resp:
        seen.update(url=url, **kw)
        return Resp()

    monkeypatch.setattr(httpx2, "post", post)
    monkeypatch.setattr(config, "load_secrets", lambda: config.Secrets({"VIGIL_API_TOKEN": "t"}))
    out = CliRunner().invoke(app, ["spec", "replay", "vi-0002", "--packet", "classic"])
    assert out.exit_code == 0, out.output
    assert seen["url"].endswith("/api/spec/replay")
    assert seen["json"] == {"job_id": "vi-0002", "packet": "classic"}
    for needle in ("rp-1", "FAILED", "no test command", "AC2", "41234", "$0.4321",
                   "cache read 20", "out 50"):  # fmt: skip
        assert needle in out.output, needle


def test_the_cli_prints_a_refusal_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    from vigild import config

    class Resp:
        status_code = 422
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {"detail": "unknown packet variant 'x'"}

    monkeypatch.setattr(httpx2, "post", lambda url, **kw: Resp())
    monkeypatch.setattr(config, "load_secrets", lambda: config.Secrets({"VIGIL_API_TOKEN": "t"}))
    out = CliRunner().invoke(app, ["spec", "replay", "vi-0002", "--packet", "x"])
    assert out.exit_code == 1 and "unknown packet variant" in out.output
