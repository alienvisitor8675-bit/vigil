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
    SPEC,
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
    plan,
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
            # tools/check.py (what the hidden test runs) is NOT written: the hidden test fails
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
    w.runner.cfg.repos.repos["o/fixture"].runner_config = ["tools/check.py"]
    r = w.client.post(
        "/api/tasks",
        headers=auth(w.tokens.admin),
        json={
            "repo": "o/fixture", "why": "w", "value": 3, "risk": 1,
            "spec": {"goal": "make the flag env visible", "test_cmd": "pytest -q tests/test_runbook.py",
                     "done_when": "d", "files_in_scope": ["tools/check.py", "tests/test_runbook.py"]},
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
            (spec.clone / "tools").mkdir(exist_ok=True)
            (spec.clone / "tools" / "check.py").write_text("print(1)\n")

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


def test_a_vitest_task_is_given_a_directory_the_runner_collects_and_its_own_contract(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    """The placement is asked of the runner (stubbed here); the author's packet names
    it; and the author's own task.json lets it write exactly there, so the harness
    commits the files (round 2, item 3)."""
    from tests.conftest import git

    seed = fixture_repo["seed"]
    (seed / "server").mkdir()
    (seed / "server" / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "a vitest project")
    git(seed, "push", "-q", "origin", "main")
    w = world
    _runner_world(w)
    job_id = _request(w)
    job = w.store.get_job(job_id)
    w.runner.verifier.collect_probe = lambda repo, image, base, cmd, files: {  # type: ignore[method-assign]
        f for f in files if f.startswith("server/test/") and f.endswith(".test.ts")
    }
    contract = {
        "task_id": f"{job_id}-w1", "job_id": job_id, "branch": f"vigil/{job_id}-w1",
        "spec": {"test_cmd": "pnpm -C server test", "files_in_scope": ["scripts/check.mjs"]},
    }  # fmt: skip
    folder = f"server/test/vigil-hidden/{job_id}/{job_id}-w1"
    seen: dict[str, Any] = {}

    def author(spec: SessionSpec, task: dict[str, Any]) -> None:
        seen["task"] = task
        d = spec.clone / folder
        d.mkdir(parents=True)
        (d / "a.test.ts").write_text("import { check } from './vigil_hidden.mjs'\ncheck(true)\n")
        m = {"test_cmd": f"pnpm -C server test {folder}", "tests": [{"name": "x", "category": "c"}]}
        (spec.clone / ".vigil" / job_id / "hidden").mkdir(parents=True, exist_ok=True)
        (spec.clone / ".vigil" / job_id / "hidden" / f"{job_id}-w1.json").write_text(json.dumps(m))

    w.standin.authors = [author, author]
    spec_md = w.store.get_artifact(job_id, "spec.md").content
    entry = w.runner._author_one(job, spec_md, contract, [])
    packets = [p for r, p in w.standin.packets if r == "hidden_test_author"]
    assert f"Write your tests under `{folder}/`" in packets[-1]
    assert "`*.test.ts`" in packets[-1]
    assert seen["task"]["spec"]["files_in_scope"] == [f"{folder}/**"]  # its own contract
    # accepted at the placement (the only thing left to refuse it is the local run: no pnpm)
    assert entry["status"] == "missing" and "outside the hidden directory" not in entry["reason"]
    assert "black-box" not in entry["reason"]


def test_a_vitest_task_whose_runner_cannot_be_asked_gets_no_author(
    world: World, fixture_repo: dict[str, Any]
) -> None:
    from tests.conftest import git

    seed = fixture_repo["seed"]
    (seed / "server").mkdir()
    (seed / "server" / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "a vitest project")
    git(seed, "push", "-q", "origin", "main")
    w = world
    _runner_world(w)
    job_id = _request(w)
    w.runner.verifier.collect_probe = lambda *a, **k: None  # type: ignore[method-assign,assignment]
    contract = {
        "task_id": f"{job_id}-w1", "job_id": job_id, "branch": f"vigil/{job_id}-w1",
        "spec": {"test_cmd": "pnpm -C server test", "files_in_scope": ["scripts/check.mjs"]},
    }  # fmt: skip
    spec_md = w.store.get_artifact(job_id, "spec.md").content
    before = [c[0] for c in w.standin.calls].count("hidden_test_author")
    entry = w.runner._author_one(w.store.get_job(job_id), spec_md, contract, [])
    assert entry["status"] == "missing" and "could not be asked" in entry["reason"]
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == before  # nothing spent


def test_a_config_overlap_is_never_authored_and_can_only_escalate(world: World) -> None:
    """The overlap includes the hidden runner's own config (conftest.py): no author
    runs, the spec item says so, and the task escalates."""
    w = world
    _runner_world(w)
    w.runner.cfg.repos.repos[REPO].runner_config = ["conftest.py"]
    w.standin.coord_drafts = [
        lambda j: (
            SPEC.replace(
                "- `tests/test_runbook.py`\n", "- `tests/test_runbook.py`\n- `conftest.py`\n"
            ),
            plan(j).replace(
                '"tests/test_feature_map.py"\n   ]',
                '"tests/test_feature_map.py",\n    "conftest.py"\n   ]',
            ),
        )
    ]
    job = _request(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 0
    (item,) = _specs(w, job)
    hidden = {x["task_id"]: x for x in item["body"]["workers"]}[f"{job}-w1"]["hidden_tests"]
    assert hidden["status"] == "missing"
    entry = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)["tasks"][f"{job}-w1"]
    assert "own config" in entry["reason"] and "conftest.py" in entry["reason"]
    w.standin.worker_edit = _flag_edit()
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_task(f"{job}-w1").state == "escalated"
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]


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


def test_a_replan_never_rewrites_a_human_written_spec(world: World) -> None:
    """The coord is asked for a new plan only; spec.md stays the human's."""
    w = world
    w.standin.worker_edit = _flaky({"w1"})  # the one worker strays out of scope once
    r = w.client.post(
        "/api/tasks",
        headers=auth(w.tokens.admin),
        json={
            "repo": REPO, "why": "w", "value": 3, "risk": 1,
            "spec": {"goal": "document the rate limit", "test_cmd": "pytest -q tests/test_feature_map.py",
                     "done_when": "d",
                     "files_in_scope": ["docs/feature-map.md", "tests/test_feature_map.py"]},
        },
    )  # fmt: skip
    assert r.status_code in (200, 202), r.text
    job = r.json()["job_id"]
    spec = w.store.get_artifact(job, "spec.md")
    assert spec.provenance == "human"
    (item,) = _specs(w, job)
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    page = _failure_item(w, job)
    assert "replan" in page["body"]["actions"]
    w.client.post(f"/api/inbox/{page['id']}/replan", headers=auth(w.tokens.admin))
    assert "coord" in [c[0] for c in w.standin.calls]  # the coord did run, for a plan
    after = w.store.get_artifact(job, "spec.md")
    assert after.content == spec.content and after.provenance == "human"
    assert w.store.get_artifact(job, "plan.md").provenance == "claude"


def test_a_replan_starts_the_author_cap_at_zero(world: World) -> None:
    """Per task per plan: the replan reuses the task id but is a new plan."""
    w = world
    _runner_world(w)
    w.standin.on_session = _book(w, 80_000, (Role.HIDDEN_TEST_AUTHOR,))  # $0.80 a session
    job_id = _request(w)
    plan = w.store.get_artifact(job_id, "plan.md")
    spec_md = w.store.get_artifact(job_id, "spec.md").content
    job = w.store.get_job(job_id)
    contract = {
        "task_id": f"{job_id}-w1", "job_id": job_id, "branch": f"vigil/{job_id}-w1",
        "spec": {"test_cmd": "pytest -q tests/test_feature_map.py", "files_in_scope": ["tools/check.py"]},
    }  # fmt: skip
    assert w.runner._author_spent(job_id, f"{job_id}-w1") == pytest.approx(0.80)
    # the same plan: a second stage on this task has $0.20 left
    # a replan: the coord writes a new plan, same task ids
    w.store.add_artifact(job_id, "plan.md", plan.content + "\n<!-- replanned -->\n", "claude")
    assert w.runner._author_spent(job_id, f"{job_id}-w1") == 0.0
    w.runner._author_one(job, spec_md, contract, [])
    amounts = [
        r["amount_usd"]
        for r in w.store.q("SELECT amount_usd FROM reservations WHERE purpose='hidden_tests'")
    ]
    assert amounts == [1.0, 1.0]


def test_a_missing_host_key_at_verify_time_escalates_instead_of_retrying(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.worker_edit = _flag_edit()
    job = _request(w)  # authored with a key
    (item,) = _specs(w, job)
    w.runner.verifier.host_key = None  # the key is gone by the time the worker is verified
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_task(f"{job}-w1").state == "escalated"
    page = _failure_item(w, job, kind="escalated")
    assert "retry" not in page["body"]["actions"] or set(page["body"]["tasks"]) != {f"{job}-w1"}
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]


def test_a_host_key_under_32_bytes_is_no_key() -> None:
    from vigild.verifier import Verifier

    v = Verifier(None, None, host_key=b"k" * 31)  # type: ignore[arg-type]
    assert v.host_key is None
    assert Verifier(None, None, host_key=b"k" * 32).host_key == b"k" * 32  # type: ignore[arg-type]


def test_a_worker_that_drops_vigilds_pre_session_commit_is_a_scope_refusal(world: World) -> None:
    import subprocess

    w = world
    _runner_world(w)
    flag = _flag_edit()

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        if str(spec.task_id).endswith("w1"):
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=spec.clone, check=True)
        flag(spec, task)

    w.standin.worker_edit = edit
    job = _approve(w)
    page = _failure_item(w, job)
    body = json.dumps(page["body"])
    assert "pre-session commit" in body and "vigild error" not in body
    assert "stage 3 acceptance failed" in body
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]
