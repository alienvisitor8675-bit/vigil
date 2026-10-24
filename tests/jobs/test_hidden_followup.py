"""Hotfix 5, runner level: the follow-up to #18's review. A worker can't make
hidden tests pass by planting files; a crash while the author's work is checked
leaves nothing live and files the task as missing; an interrupted author is
re-run alone, never by the coord; one open spec item per job; a retry still
runs the job's other failed tasks when one is escalated."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.jobs.test_coord_loop import (  # noqa: F401
    HIDDEN_NAME,
    REPO,
    World,
    _approve,
    _events,
    _failure_item,
    _flag_edit,
    _flaky,
    _request,
    _runner_world,
    _spec_item,
    _work,
    auth,
    failed_author,
    good_author,
    world,
)
from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.jobs.model import Role

PLUGIN = """import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    r = out.get_result()
    if r.when == "call":
        r.outcome = "passed"
        r.longrepr = None


def pytest_collection_modifyitems(items):
    for it in items:
        it._nodeid = "{prefix}/" + it._nodeid
"""


def _specs(w: World, job: str) -> list[dict[str, Any]]:
    items = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    return [s for s in items if s["job_id"] == job]


def _pages(w: World, job: str) -> list[dict[str, Any]]:
    items = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    return [q for q in items if q["job_id"] == job]


def test_worker_planted_ini_above_the_task_dir_no_longer_makes_hidden_tests_pass(
    world: World,
) -> None:
    """The reviewer's reproduction: `.vigil/<job>/hidden/pytest.ini` with
    `addopts = -p flipper` and the plugin that flips every result."""
    w = world
    _runner_world(w)

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if str(spec.task_id).endswith("w1"):
            # the conftest.py the hidden test needs is NOT written: the hidden test fails
            (spec.clone / "conftest.py").write_text("# unchanged behaviour\n")
            d = spec.clone / ".vigil" / spec.job_id / "hidden"
            d.mkdir(parents=True, exist_ok=True)
            (d / "pytest.ini").write_text("[pytest]\npythonpath = .\naddopts = -p flipper\n")
            (d / "flipper.py").write_text(
                PLUGIN.replace("{prefix}", f".vigil/{spec.job_id}/hidden")
            )

    w.standin.worker_edit = edit
    job = _approve(w)
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert rec["hidden"]["ok"] is False and "tampered" not in rec["hidden"]  # no effect at all
    assert rec["hidden"]["head"]["outcome"] == "fail"
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs], "the plant worked"
    assert w.store.get_task(f"{job}-w1").state == "failed"
    page = _failure_item(w, job)
    assert f".vigil/{job}/hidden/pytest.ini" in json.dumps(page["body"])  # and the scope refuses it


def test_a_verifier_crash_during_the_authors_check_fails_the_session_and_files_the_task(
    world: World,
) -> None:
    """The reviewer's reproduction: the session stayed `running`, a restart paged
    "coord was drafting" with retry, and the retry ran the coord again."""
    w = world
    _runner_world(w)

    def boom(**kw: Any) -> Any:
        raise RuntimeError("docker went away")

    w.runner.verifier.check_hidden_on_base = boom  # type: ignore[method-assign]
    job = _request(w)
    (item,) = _specs(w, job)
    hidden = {x["task_id"]: x for x in item["body"]["workers"]}[f"{job}-w1"]["hidden_tests"]
    assert hidden["status"] == "missing"
    assert hidden["note"] == "hidden tests missing; this task can only escalate"
    sessions = [(s.role, s.status) for s in w.store.list_sessions(job)]
    assert (Role.HIDDEN_TEST_AUTHOR, "running") not in sessions
    assert (Role.HIDDEN_TEST_AUTHOR, "failed") in sessions
    assert w.budget.open_today() == 0
    # a restart finds nothing live: no page, and no second spec item
    w.runner.reconcile()
    assert _pages(w, job) == [] and len(_specs(w, job)) == 1
    assert [c[0] for c in w.standin.calls].count("coord") == 1


def _interrupted_mid_author(w: World, job: str) -> None:
    """The state a kill during the author leaves: no spec item, a live author."""
    (item,) = _specs(w, job)
    w.store.resolve_inbox_item(item["id"], "test")
    (author,) = [s for s in w.store.list_sessions(job) if s.role is Role.HIDDEN_TEST_AUTHOR]
    w.store.update_session(author.id, status="running")


def test_reconcile_reruns_only_the_author_step_after_a_crash_mid_author(world: World) -> None:
    w = world
    _runner_world(w)
    job = _request(w)
    _interrupted_mid_author(w, job)
    spec_before = w.store.get_artifact(job, "spec.md").content
    w.runner.reconcile()
    (page,) = _pages(w, job)
    assert "hidden-test author" in page["title"] and "retry" in page["body"]["actions"]
    before = [c[0] for c in w.standin.calls]
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    after = [c[0] for c in w.standin.calls]
    assert after.count("coord") == before.count("coord"), "the coord ran again"
    assert after.count("hidden_test_author") == before.count("hidden_test_author") + 1
    assert len(_specs(w, job)) == 1  # one open spec item for the job
    assert w.store.get_artifact(job, "spec.md").content == spec_before


def test_a_retry_never_hands_a_human_written_spec_to_the_coord(world: World) -> None:
    w = world
    _runner_world(w)
    w.runner.cfg.repos.repos["o/fixture"].runner_config = ["conftest.py"]
    r = w.client.post(
        "/api/tasks",
        headers=auth(w.tokens.admin),
        json={
            "repo": "o/fixture", "why": "w", "value": 3, "risk": 1,
            "spec": {"goal": "make the flag env visible", "test_cmd": "pytest -q tests/test_runbook.py",
                     "done_when": "d", "files_in_scope": ["conftest.py", "tests/test_runbook.py"]},
        },
    )  # fmt: skip
    assert r.status_code in (200, 202), r.text
    job = r.json()["job_id"]
    spec = w.store.get_artifact(job, "spec.md")
    assert spec.provenance == "human" and len(_specs(w, job)) == 1
    _interrupted_mid_author(w, job)
    w.runner.reconcile()
    (page,) = _pages(w, job)
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert "coord" not in [c[0] for c in w.standin.calls]
    assert w.store.get_artifact(job, "spec.md").content == spec.content
    assert len(_specs(w, job)) == 1


def test_one_open_spec_item_per_job_is_enforced(world: World) -> None:
    w = world
    _runner_world(w)
    job = _request(w)
    assert len(_specs(w, job)) == 1
    assert w.runner.jobs is not None
    w.runner.jobs.file_item(job)
    w.runner.jobs.file_item(job)
    assert len(_specs(w, job)) == 1
    n = len([c for c in w.standin.calls if c[0] == "hidden_test_author"])
    w.runner._finish_spec(job)  # a second finish with an item open authors nothing
    assert len([c for c in w.standin.calls if c[0] == "hidden_test_author"]) == n


def test_retry_still_runs_the_other_failed_tasks_when_one_is_escalated(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.authors = [failed_author, failed_author]  # w1 has no hidden tests: it escalates
    flaky = _flaky({"w2"})

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        flaky(spec, task)
        if str(spec.task_id).endswith("w1"):
            (spec.clone / "conftest.py").write_text("import os\n")

    w.standin.worker_edit = edit
    job = _approve(w)
    assert w.store.get_task(f"{job}-w1").state == "escalated"
    assert w.store.get_task(f"{job}-w2").state == "failed"
    page = _failure_item(w, job, kind="escalated")
    assert set(page["body"]["tasks"]) == {f"{job}-w1", f"{job}-w2"}
    assert page["body"]["actions"] == ["retry", "replan", "abandon"]
    workers = lambda: [c[1] for c in w.standin.calls if c[0] == "worker"]  # noqa: E731
    assert workers().count(f"{job}-w1") == 1
    r = w.client.post(f"/api/inbox/{page['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert workers().count(f"{job}-w1") == 1, "the escalated task ran again"
    assert workers().count(f"{job}-w2") == 2
    assert f"vigil/{job}-w2" in [p["head"] for p in w.publisher.prs]
    # the escalated task still holds the job, and only a new plan helps it
    last = _failure_item(w, job, kind="escalated")
    assert last["body"]["actions"] == ["replan", "abandon"]
    assert set(last["body"]["tasks"]) == {f"{job}-w1"}
    assert w.store.get_job(job).state.value == "escalated"
    assert [e["task"] for e in _events(w, "escalated")] == [f"{job}-w1"]


def test_a_vitest_task_is_given_a_directory_its_include_collects(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    """The base's vitest config is read through vigild's own path, the author's
    packet names the directory, and the author's files are accepted only there."""
    from tests.conftest import git

    seed = fixture_repo["seed"]
    (seed / "server").mkdir()
    (seed / "server" / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
    (seed / "server" / "vitest.config.ts").write_text(
        "export default { test: { include: ['test/**/*.test.ts'] } }\n"
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "a vitest project")
    git(seed, "push", "-q", "origin", "main")
    w = world
    _runner_world(w)
    job_id = _request(w)
    job = w.store.get_job(job_id)
    contract = {
        "task_id": f"{job_id}-w1", "job_id": job_id, "branch": f"vigil/{job_id}-w1",
        "spec": {"test_cmd": "pnpm -C server test", "files_in_scope": ["conftest.py"]},
    }  # fmt: skip
    folder = f"server/test/vigil-hidden/{job_id}/{job_id}-w1"

    def author(spec: SessionSpec, task: dict[str, Any]) -> None:
        d = spec.clone / folder
        d.mkdir(parents=True)
        (d / "a.test.ts").write_text("import { test } from 'vitest'\ntest('x', () => {})\n")
        m = {"test_cmd": f"pnpm -C server test {folder}", "tests": [{"name": "x", "category": "c"}]}
        (spec.clone / ".vigil" / job_id / "hidden").mkdir(parents=True, exist_ok=True)
        (spec.clone / ".vigil" / job_id / "hidden" / f"{job_id}-w1.json").write_text(json.dumps(m))

    w.standin.authors = [author, author]
    spec_md = w.store.get_artifact(job_id, "spec.md").content
    entry = w.runner._author_one(job, spec_md, contract, [])
    packets = [p for r, p in w.standin.packets if r == "hidden_test_author"]
    assert f"Write your tests under `{folder}/`" in packets[-1]
    assert "`test/**/*.test.ts`" in packets[-1]
    # accepted where the include collects them; refused only by the (local, no pnpm) run
    assert entry["status"] == "missing" and "outside the hidden directory" not in entry["reason"]
    assert "load" not in entry["reason"]


def _book(w: World, tokens_out: int, roles: tuple[Role, ...]) -> Any:
    """Each session of these roles books Sonnet output tokens ($10 per million)."""
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    prices = PriceTable(w.runner.cfg.prices)

    def book(spec: SessionSpec) -> None:
        if spec.role in roles:
            record_call(
                w.store, prices, session_id=spec.session_id, task_id=None, job_id=spec.job_id,
                purpose="session", provider="anthropic", backend_preset=spec.preset,
                model=spec.model, usage=CallUsage(tokens_out=tokens_out),
            )  # fmt: skip

    return book


# --- owner changes, 2026-10-23 ----------------------------------------------------------------


def test_without_the_host_key_every_flagged_task_escalates(world: World) -> None:
    w = world
    _runner_world(w)
    w.runner.verifier.host_key = None
    w.standin.worker_edit = _flag_edit()
    job = _request(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 0  # no session, no spend
    (item,) = _specs(w, job)
    hidden = {x["task_id"]: x for x in item["body"]["workers"]}[f"{job}-w1"]["hidden_tests"]
    assert hidden["status"] == "missing"
    entry = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)["tasks"][f"{job}-w1"]
    assert entry["reason"] == "VIGIL_HOST_KEY is not set"


def test_a_worker_that_edits_a_file_vigild_committed_is_refused(world: World) -> None:
    w = world

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        (spec.clone / ".vigil" / spec.job_id / "spec.md").write_text("rewritten\n")

    w.standin.worker_edit = edit
    job = _approve(w)
    page = _failure_item(w, job)
    assert "files changed outside scope" in json.dumps(page["body"])
    assert f".vigil/{job}/spec.md" in json.dumps(page["body"])
    assert w.publisher.prs == []


def test_the_author_stage_is_capped_at_one_dollar_including_the_regeneration(
    world: World,
) -> None:
    w = world
    _runner_world(w)
    # the first try fails the wrong way after spending $0.80 of Sonnet output
    bad = good_author("import nothing_here_exists\n\n\ndef test_x():\n    pass\n")
    w.standin.authors = [bad, bad]
    book = _book(w, 80_000, (Role.HIDDEN_TEST_AUTHOR,))  # $0.80 a try
    caps: list[float] = []

    def on_session(spec: SessionSpec) -> None:
        if spec.role is Role.HIDDEN_TEST_AUTHOR:
            caps.append(spec.max_budget_usd)
        book(spec)

    w.standin.on_session = on_session
    job = _request(w)
    sessions = [s for s in w.store.list_sessions(job) if s.role is Role.HIDDEN_TEST_AUTHOR]
    rows = w.store.q("SELECT amount_usd FROM reservations WHERE purpose='hidden_tests' ORDER BY id")
    amounts = [r["amount_usd"] for r in rows]
    assert amounts[0] == 1.0
    assert len(amounts) == 2 and amounts[1] == pytest.approx(0.20, abs=0.001)  # what is left
    assert caps == [1.0, pytest.approx(0.20, abs=0.001)]  # the session is told what is left
    assert len(sessions) == 2
    assert w.budget.open_today() == 0


def test_a_regeneration_with_nothing_left_of_the_cap_is_refused(world: World) -> None:
    w = world
    _runner_world(w)
    bad = good_author("import nothing_here_exists\n\n\ndef test_x():\n    pass\n")
    w.standin.authors = [bad, bad]
    w.standin.on_session = _book(w, 99_000, (Role.HIDDEN_TEST_AUTHOR,))  # $0.99
    job = _request(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 1
    entry = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)["tasks"][f"{job}-w1"]
    assert entry["status"] == "missing" and "cap" in entry["reason"]


def test_the_spec_item_shows_author_spend_next_to_coord_spend(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.on_session = _book(w, 10_000, (Role.COORD, Role.HIDDEN_TEST_AUTHOR))
    job = _request(w)
    (item,) = _specs(w, job)
    spend = item["body"]["spend"]
    assert set(spend) == {"coord_usd", "hidden_test_author_usd"}
    assert spend["coord_usd"] > 0 and spend["hidden_test_author_usd"] > 0


def test_a_private_repo_other_than_vigil_is_refused_hosted_authoring(cfg: Config) -> None:
    from vigild.repos import RepoPolicyError, check_hidden_author

    sonnet = cfg.routing.reviewer
    assert cfg.backends.presets[sonnet].locality == "hosted"
    any_repo = next(iter(cfg.repos.repos.values()))
    for name, vis in (
        ("o/secret", "private"),
        ("thomasbardhi01/vigil", "private"),
        ("o/open", "public"),
    ):
        cfg.repos.repos[name] = any_repo.model_copy(
            update={"visibility": vis, "backends": [sonnet], "worker_overflow": []}
        )
    with pytest.raises(RepoPolicyError, match="hidden-test authoring"):
        check_hidden_author(cfg, "o/secret", sonnet)
    check_hidden_author(cfg, "thomasbardhi01/vigil", sonnet)  # the waiver, in code
    check_hidden_author(cfg, "o/open", sonnet)


def test_the_runner_files_a_private_repo_as_missing(world: World) -> None:
    w = world
    _runner_world(w)
    pol = w.runner.cfg.repos.repos[REPO]
    w.runner.cfg.repos.repos[REPO] = pol.model_copy(update={"visibility": "private"})
    job = _request(w)
    entry = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)["tasks"][f"{job}-w1"]
    assert entry["status"] == "missing" and "hidden-test authoring" in entry["reason"]
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 0
