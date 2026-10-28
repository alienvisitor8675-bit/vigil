"""Review replay (#47): `POST /api/review/replay` runs one reviewer session on a past
job's task head with a chosen prompt, through vigild's own gateway and runtime. It
books to the experiment line, writes only `eval-only` rows, and changes nothing a
real review changes (ladder, job, task, inbox, GitHub)."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from tests.bench.conftest import NoGitHub, _pem
from tests.conftest import auth
from tests.jobs.test_coord_loop import CLEAN_REVIEW, World, _approve
from tests.jobs.test_coord_loop import world as world
from vigild.breakers import GLOBAL, Kind
from vigild.contract.models import ReviewOutput
from vigild.executors.base import SessionSpec
from vigild.github.app import GitHubApp
from vigild.jobs.model import Role
from vigild.jobs.runner import GitHubPRs, GitHubPublisher
from vigild.metering.calls import CallUsage, record_call
from vigild.metering.prices import PriceTable
from vigild.packets.assemble import assemble_review
from vigild.repos import set_visibility_source

FINDING = {"severity": "medium", "file": "docs/feature-map.md", "line": 3,
           "claim": "AC1: the row names no unit", "fix": "say req/min"}  # fmt: skip
CHANGES = {**CLEAN_REVIEW, "verdict": "changes", "findings": [FINDING]}


def _ready(w: World) -> tuple[str, str]:
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    return job, f"{job}-w1"


def _post(w: World, job: str, task: str, version: str = "new", **extra: Any) -> Any:
    return w.client.post(
        "/api/review/replay",
        headers=auth(w.tokens.admin),
        json={"job_id": job, "task_id": task, "prompt_version": version, **extra},
    )


def _spender(w: World, tokens_out: int = 15_000) -> Callable[[SessionSpec], Any]:
    """A reviewer that spends Opus output tokens through the meter, then answers."""
    prices = PriceTable(w.runner.cfg.prices)

    def review(spec: SessionSpec) -> Any:
        record_call(
            w.store, prices, session_id=spec.session_id, task_id=None, job_id=None,
            purpose="session", provider="anthropic", backend_preset="claude-opus",
            model="claude-opus-5-5", usage=CallUsage(tokens_out=tokens_out),
        )  # fmt: skip
        return CHANGES

    return review


PROV_TABLES = [
    "jobs",
    "tasks",
    "sessions",
    "events",
    "artifacts",
    "inbox_items",
    "approvals",
    "model_calls",
    "decisions",
    "ladder",
    "audits",
    "ladder_changes",
    "breakers",
    "transcripts",
    "intake_seen",
    "jev_drift",
]
ALL_TABLES = (*PROV_TABLES, "reservations", "ledger", "ui_sessions", "dev_images")


def _dump(store: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        t: [dict(r) for r in store.q(f"SELECT * FROM {t} ORDER BY rowid")]  # noqa: S608
        for t in ALL_TABLES
    }


def _replay_count(store: Any) -> int:
    return int(store.q("SELECT COUNT(*) FROM replays")[0][0])


# --- the route ------------------------------------------------------------------------


def test_admin_only(world: World, prompts: dict[str, str]) -> None:
    w = world
    job, task = _ready(w)
    body = {"job_id": job, "task_id": task, "prompt_version": "new"}
    assert w.client.post("/api/review/replay", json=body).status_code == 401
    for token in (w.tokens.read, w.tokens.heartbeat):
        r = w.client.post("/api/review/replay", headers=auth(token), json=body)
        assert r.status_code == 403
    assert _replay_count(w.store) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"task_id": "x-w1", "prompt_version": "new"},
        {"job_id": "x", "prompt_version": "new"},
        {"job_id": "x", "task_id": "x-w1"},
        {"job_id": "x", "task_id": "x-w1", "prompt_version": ""},
        {"job_id": "x", "task_id": "x-w1", "prompt_version": "new", "visibility": "public"},
        {"job_id": "x", "task_id": "x-w1", "prompt_version": "new", "private": False},
        {"job_id": "x", "task_id": "x-w1", "prompt_version": "new", "head": "not-a-sha"},
        {"job_id": 7, "task_id": "x-w1", "prompt_version": "new"},
    ],
)
def test_the_body_is_validated(world: World, prompts: dict[str, str], body: dict[str, Any]) -> None:
    r = world.client.post("/api/review/replay", headers=auth(world.tokens.admin), json=body)
    assert r.status_code == 422, r.text


def test_unknown_things_are_refused_before_anything_runs(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    before = len(w.standin.specs)
    assert _post(w, "nope-0001", task).status_code == 404
    assert _post(w, job, "nope-w9").status_code == 404
    assert _post(w, job, f"{job}-w9").status_code == 404
    assert _post(w, job, task, "no-such-ref").status_code == 422
    assert _post(w, job, task, "0" * 64).status_code == 422  # a hash no shipped prompt has
    assert _post(w, job, task, "--output=/tmp/x").status_code == 422  # never a git option
    assert _post(w, job, task, "new", head="a" * 40).status_code == 422  # not a head it published
    assert len(w.standin.specs) == before
    assert _replay_count(w.store) == 0


def test_a_replay_returns_its_id_verdict_findings_and_cost(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    w.standin.review = _spender(w)
    r = _post(w, job, task, "new")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"].startswith("rp-") and out["status"] == "done"
    assert out["verdict"] == "changes" and out["findings"] == [FINDING]
    assert out["cost_usd"] == pytest.approx(0.30)  # 15,000 Opus output tokens at $20/M
    assert out["provenance"] == "eval-only"
    assert out["prompt_sha256"] == prompts["new"]
    (spec,) = [s for s in w.standin.specs if s.session_id in _replay_sessions(w)]
    assert spec.role is Role.REVIEWER and "NEW-PROMPT-MARKER" in spec.packet
    # by content hash, and by an older ref: the packet carries that prompt instead
    r2 = _post(w, job, task, prompts["old"])
    assert r2.status_code == 200 and r2.json()["prompt_sha256"] == prompts["old"]
    assert "OLD-PROMPT-MARKER" in w.standin.specs[-1].packet
    assert "NEW-PROMPT-MARKER" not in w.standin.specs[-1].packet


def _replay_sessions(w: World) -> set[str]:
    return {str(r[0]) for r in w.store.q("SELECT session_id FROM replays")}


# --- money: the experiment line -------------------------------------------------------


def test_it_books_only_to_the_experiment_line_and_counts_toward_the_month(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    w.standin.review = _spender(w)
    day, held = w.budget.spent_today(), w.budget.open_today()
    month, exp = w.budget.spent_month(), w.budget.spent_experiment_month()
    assert _post(w, job, task).status_code == 200
    assert w.budget.spent_today() == pytest.approx(day)  # the $5 day is unchanged
    assert w.budget.open_today() == pytest.approx(held)
    assert w.budget.spent_experiment_month() == pytest.approx(exp + 0.30)
    assert w.budget.spent_month() == pytest.approx(month + 0.30)  # always in the month
    (sid,) = _replay_sessions(w)
    res = w.store.q("SELECT amount_usd, pool, status FROM reservations WHERE session_id=?", (sid,))
    assert [(r["pool"], r["status"]) for r in res] == [("experiment", "settled")]
    assert res[0]["amount_usd"] == pytest.approx(w.runner.cfg.budgets.review_session_usd)
    led = w.store.q("SELECT pool, amount_usd FROM ledger WHERE session_id=?", (sid,))
    assert [r["pool"] for r in led] == ["experiment"]


def test_a_replay_is_capped_at_one_review_session(world: World, prompts: dict[str, str]) -> None:
    """Unlike a real review, which may run past its cap, a replay's cap is hard."""
    w = world
    job, task = _ready(w)
    seen: list[bool] = []

    def probe(spec: SessionSpec) -> Any:
        seen.append(w.runner.gateway._over_cap_ok(spec.session_id, 0.05) is False)
        return CLEAN_REVIEW

    w.standin.review = probe
    assert _post(w, job, task).status_code == 200
    assert seen == [True]
    # a real reviewer session still gets its over-cap extension
    real = w.store.create_session(job, Role.REVIEWER)
    w.budget.reserve(session_id=real.id, task_id=None, amount=0.4, purpose="reviewer")
    assert w.runner.gateway._over_cap_ok(real.id, 0.05) is True


def test_a_full_experiment_line_refuses(world: World, prompts: dict[str, str]) -> None:
    w = world
    job, task = _ready(w)
    w.runner.cfg.budgets.experiment_monthly_usd = 0.30  # under one review_session_usd
    day, month = w.budget.spent_today(), w.budget.spent_month()
    specs = len(w.standin.specs)
    r = _post(w, job, task)
    assert r.status_code == 409 and "experiment" in r.text
    assert len(w.standin.specs) == specs  # no session ran
    assert w.budget.spent_today() == day and w.budget.spent_month() == month
    assert w.budget.open_experiment() == 0.0
    (row,) = w.store.q("SELECT status, provenance FROM replays")
    assert (row["status"], row["provenance"]) == ("refused", "eval-only")


# --- no effects -----------------------------------------------------------------------


def test_every_row_it_writes_is_eval_only_and_nothing_else_moves(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    w.standin.review = _spender(w)
    before = _dump(w.store)
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()
    ladder = w.client.get("/api/review/ladder", headers=auth(w.tokens.read)).json()
    sessions_before = len(w.standin.specs)
    assert _post(w, job, task).status_code == 200
    after = _dump(w.store)
    for table, old in before.items():
        assert after[table][: len(old)] == old, f"{table}: an existing row changed"
        new = after[table][len(old) :]
        if table in PROV_TABLES:
            assert {r["provenance"] for r in new} <= {"eval-only"}, table
        if table in ("reservations", "ledger"):
            assert {r["pool"] for r in new} <= {"experiment"}, table
    assert after["sessions"][len(before["sessions"]) :], "the replay left no session row"
    # one reviewer session ran: no auditor (rung 0 audits every real review), no worker
    assert [s.role for s in w.standin.specs[sessions_before:]] == [Role.REVIEWER]
    # the ladder, rungs, windows and audits used for promotion
    for table in ("ladder", "ladder_changes", "audits"):
        assert after[table] == before[table]
    assert w.client.get("/api/review/ladder", headers=auth(w.tokens.read)).json() == ladder
    # the job and task, the inbox, the job's own review.json
    assert after["jobs"] == before["jobs"] and after["tasks"] == before["tasks"]
    assert after["inbox_items"] == before["inbox_items"]
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json() == inbox
    assert after["artifacts"] == before["artifacts"]
    assert w.store.get_job(job).state.value == "ready"
    # its own result is in the replay table, eval-only
    (row,) = w.store.q("SELECT * FROM replays")
    assert row["provenance"] == "eval-only" and row["job_id"] == job and row["task_id"] == task
    assert json.loads(row["findings"]) == [FINDING] and row["verdict"] == "changes"


def test_a_replay_makes_no_github_call(
    world: World, prompts: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job, task = _ready(w)
    net = NoGitHub()
    http = httpx2.Client(transport=httpx2.MockTransport(net))
    app = GitHubApp("1", _pem(), http=http, claude_mods="o/mods", allowed_repos=["o/fixture"])
    w.runner.publisher = GitHubPublisher(app, http, w.runner.workspace)  # type: ignore[assignment]
    w.runner.pr_state = GitHubPRs(app).state

    def no_network(self: socket.socket, address: Any) -> None:
        raise AssertionError(f"a replay opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    w.standin.review = _spender(w)
    assert _post(w, job, task).status_code == 200
    assert net.requests == []


@pytest.mark.parametrize(
    ("kind", "scope"),
    [(Kind.DAILY, GLOBAL), (Kind.WIP, "other/repo"), (Kind.PER_TASK, "task:zz-0001-w1")],
)
def test_any_open_breaker_refuses(
    world: World, prompts: dict[str, str], kind: Kind, scope: str
) -> None:
    w = world
    job, task = _ready(w)
    w.runner.breakers.trip(kind, scope, {"why": "test"})
    specs, reservations = len(w.standin.specs), _dump(w.store)["reservations"]
    r = _post(w, job, task)
    assert r.status_code == 409 and "breaker" in r.text
    assert len(w.standin.specs) == specs
    assert _dump(w.store)["reservations"] == reservations
    assert _replay_count(w.store) == 0


def test_a_private_repo_never_reaches_a_backend_the_rule_forbids(
    world: World, prompts: dict[str, str]
) -> None:
    """No visibility parameter: the repo's visibility is read live as for a real review,
    and fixture repos list hosted backends, which a private repo may not use."""
    w = world
    job, task = _ready(w)
    set_visibility_source(lambda repo: True)
    specs = len(w.standin.specs)
    r = _post(w, job, task)
    assert r.status_code == 403 and "private" in r.text
    assert len(w.standin.specs) == specs
    assert w.budget.open_experiment() == 0.0
    # the same call with the repo public runs
    set_visibility_source(lambda repo: False)
    assert _post(w, job, task).status_code == 200


def test_a_private_repo_is_refused_at_the_session_too(
    world: World, prompts: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the pre-check were skipped, the session path checks the backend (the
    same `check_backend` a real review uses) before it reserves or starts anything."""
    w = world
    job, task = _ready(w)
    set_visibility_source(lambda repo: True)
    monkeypatch.setattr("vigild.jobs.runner.repo_rule_errors", lambda cfg, repo, private: [])
    specs = len(w.standin.specs)
    r = _post(w, job, task)
    assert r.status_code == 403
    assert len(w.standin.specs) == specs and w.budget.open_experiment() == 0.0
    (row,) = w.store.q("SELECT status FROM replays")
    assert row["status"] == "refused"


# --- production review is unchanged ---------------------------------------------------


def _same(packet: str) -> str:
    """A packet with its fences' random nonces blanked (each assembly draws new ones)."""
    return re.sub(r"(<<<(?:UNTRUSTED|END) [a-z0-9_-]+) [0-9a-f]{16}>>>", r"\1 N>>>", packet)


def _legacy_reviewer_packet(w: World, job_id: str, task_id: str, head: str) -> str:
    """The packet `_review_one` built before the shared helper was factored out, written
    out here as it was (same inputs, same order), so a refactor can't change it."""
    runner = w.runner
    job, t = w.store.get_job(job_id), w.store.get_task(task_id)
    base_sha = runner.workspace.base_sha(t.id)
    diff = runner.workspace.full_diff(job.repo, base_sha, head)
    tests = w.store.get_artifact(job.id, "tests.json", t.id)
    spec_md = w.store.get_artifact(job.id, "spec.md")
    report = w.store.get_artifact(job.id, "worker_report.json", t.id)
    request = {
        "job_id": job.id,
        "task_id": t.id,
        "round": 1,
        "head": head,
        "base_sha": base_sha,
        "contract": t.contract,
        "verifier": json.loads(tests.content) if tests else None,
        "spec_md": spec_md.content if spec_md else "",
    }
    untrusted = {
        "the request": job.request,
        "the worker's report": report.content if report else "(none)",
    }
    rules = runner.workspace.read_blob(job.repo, base_sha, "CLAUDE.md") or ""
    return assemble_review(rules, request, diff=diff, untrusted=untrusted).text


def test_the_production_review_packet_and_session_are_what_they_were(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    head = w.runner._task_head(task)
    assert head
    real = [s for s in w.standin.specs if s.role is Role.REVIEWER and s.task_id == task]
    assert len(real) == 1
    assert _same(real[0].packet) == _same(_legacy_reviewer_packet(w, job, task, head))
    assert real[0].max_budget_usd == w.runner.cfg.budgets.daily_usd
    assert real[0].max_output_tokens == 8192 and real[0].output_schema == (
        ReviewOutput.model_json_schema()
    )
    # a real review still draws from the job's review stage, in the ops pool
    rows = w.store.q(
        "SELECT pool, task_id FROM reservations WHERE session_id=?", (real[0].session_id,)
    )
    assert rows and {r["pool"] for r in rows} == {"ops"} and {r["task_id"] for r in rows} == {None}
    assert w.store.get_session(real[0].session_id).job_id == job
    # and every row of it is the claude tag, not eval-only
    prov = w.store.q("SELECT provenance FROM sessions WHERE id=?", (real[0].session_id,))
    assert prov[0]["provenance"] == "claude"


def test_a_replay_with_todays_prompt_is_the_same_session_as_the_real_review(
    world: World, prompts: dict[str, str]
) -> None:
    """Same packet assembly, image, read-only tools, model route and limits."""
    w = world
    job, task = _ready(w)
    (real,) = [s for s in w.standin.specs if s.role is Role.REVIEWER and s.task_id == task]
    assert _post(w, job, task, "prod").status_code == 200
    replay = w.standin.specs[-1]
    assert replay.session_id != real.session_id
    assert _same(replay.packet) == _same(real.packet)
    for field in ("role", "job_id", "task_id", "repo", "preset", "model", "image",
                  "max_turns", "max_minutes", "max_budget_usd", "output_schema",
                  "max_output_tokens"):  # fmt: skip
        assert getattr(replay, field) == getattr(real, field), field
    assert [(m.container, m.read_only) for m in replay.mounts] == [
        (m.container, m.read_only) for m in real.mounts
    ]
    assert replay.clone != real.clone and replay.session_dir != real.session_dir


def test_a_replay_of_an_earlier_head_uses_that_heads_round(
    world: World, prompts: dict[str, str]
) -> None:
    w = world
    job, task = _ready(w)
    head = w.runner._task_head(task)
    assert _post(w, job, task, "new", head=head).status_code == 200
    assert f'"head": "{head}"' in w.standin.specs[-1].packet


def test_a_replays_settle_runs_no_global_spend_check(
    world: World, prompts: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay never touches real breaker state: its settle skips the day/month check."""
    w = world
    job, task = _ready(w)
    w.standin.review = _spender(w)
    checks: list[int] = []
    monkeypatch.setattr(w.runner, "_spend_check", lambda: checks.append(1))
    assert _post(w, job, task).status_code == 200
    assert checks == []
