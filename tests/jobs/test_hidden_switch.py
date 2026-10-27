"""The host-policy switch `hidden_tests.enabled` (owner, 2026-10-26): off for every
repo. Off, nothing reaches hidden-test code: no author, no reservation for one, no
hidden run, no tag. A task whose scope names a runner file is built and verified on
its visible tests, then opens a draft PR for the owner to review, and no job is
left escalated without a PR. It never depends on VIGIL_HOST_KEY."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.jobs.test_coord_loop import (  # noqa: F401
    REPO,
    World,
    _approve,
    _flag_edit,
    _named,
    _request,
    _spec_item,
    auth,
    world,
)
from vigild import config as config_mod
from vigild.config import HiddenTestsPolicy
from vigild.jobs.model import Role

TITLE = "Owner review required: runner file changed; hidden tests disabled"


def _flagged_world(w: World) -> None:
    """A repo whose runner file tools/check.py the first worker's scope names; the
    switch is left at its default."""
    w.runner.cfg.repos.repos[REPO].runner_config = ["tools/check.py"]
    w.standin.coord_drafts = [_named]


def _boom(*a: Any, **k: Any) -> Any:
    raise AssertionError("hidden-test code was reached with the switch off")


def _tripwires(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(w.runner), "_author_hidden_tests", _boom)
    monkeypatch.setattr(type(w.runner), "_author_one", _boom)
    monkeypatch.setattr(type(w.runner.verifier), "check_hidden_on_base", _boom)
    monkeypatch.setattr(type(w.runner.verifier), "collect_probe", _boom)
    monkeypatch.setattr("vigild.verifier.hidden_tag", _boom)
    monkeypatch.setattr("vigild.jobs.runner.assemble_hidden_test_author", _boom)


def test_every_repo_has_the_switch_off_by_default() -> None:
    cfg = config_mod.load()
    assert cfg.repos.repos
    assert all(p.hidden_tests.enabled is False for p in cfg.repos.repos.values())
    assert HiddenTestsPolicy().enabled is False


def test_off_no_hidden_code_runs_and_the_spec_is_filed_without_an_author(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    _flagged_world(w)
    _tripwires(w, monkeypatch)
    job = _request(w)
    assert [c[0] for c in w.standin.calls] == ["coord"]  # no author session
    item = _spec_item(w)
    w1 = {x["task_id"]: x for x in item["body"]["workers"]}[f"{job}-w1"]
    assert w1["runner_file_change"] == ["tools/check.py"]
    assert w1["hidden_tests"]["status"] == "disabled"
    assert w.store.get_artifact(job, "hidden_tests.json") is None
    assert not [s for s in w.store.list_sessions(job) if s.role is Role.HIDDEN_TEST_AUTHOR]
    assert not w.store.q("SELECT id FROM reservations WHERE purpose='hidden_tests'")


def test_off_a_flagged_task_opens_the_owner_review_draft_pr_and_nothing_escalates(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    _flagged_world(w)
    _tripwires(w, monkeypatch)
    w.runner.verifier.host_key = None  # a missing VIGIL_HOST_KEY does not matter
    w.standin.worker_edit = _flag_edit()
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"  # then 2a's review: clean
    pr = next(p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w1")
    assert pr["title"] == TITLE and pr["draft"] is True
    assert "`tools/check.py`" in pr["body"] and "Hidden tests are disabled" in pr["body"]
    other = next(p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w2")
    assert other["title"] != TITLE and "Owner review required" not in other["body"]
    assert w.store.get_task(f"{job}-w1").state == "reviewed"
    assert w.store.get_task(f"{job}-w1").contract["runner_file_change"] == ["tools/check.py"]
    # a runner-file task is reviewed and audited at rung 0, and its audit counts for
    # nothing: only the other task's audit is in the ladder's tally (finding #2)
    assert w.store.get_artifact(job, "audit.json", task_id=f"{job}-w1") is not None
    assert [r["task_id"] for r in w.store.q("SELECT task_id FROM audits")] == [f"{job}-w2"]
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert "hidden" not in rec and rec["runner_files"] == ["tools/check.py"]
    assert rec["after"]["outcome"] == "pass"  # verified on its visible tests
    states = {t.state for t in w.store.list_tasks(job)}
    assert "escalated" not in states and w.store.get_job(job).state.value != "escalated"
    (opened,) = [
        json.loads(e["data"])
        for e in w.store.q(
            "SELECT data FROM events WHERE job_id=? AND type='pr_open' AND task_id=?",
            (job, f"{job}-w1"),
        )
    ]
    assert opened["runner_file_change"] == ["tools/check.py"]  # the marker stays: rung 0


def test_off_a_failing_visible_test_still_fails_the_task(world: World) -> None:
    w = world
    _flagged_world(w)

    def edit(spec: Any, task: Any) -> None:
        from tests.jobs.test_coord_loop import _work

        _work(spec.clone, task["spec"]["files_in_scope"][0], honest=False)

    w.standin.worker_edit = edit
    job = _approve(w)
    assert not [p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w1"]


def test_on_the_author_still_runs(world: World) -> None:
    w = world
    _flagged_world(w)
    w.runner.cfg.repos.repos[REPO].hidden_tests = HiddenTestsPolicy(enabled=True)
    _request(w)
    assert [c[0] for c in w.standin.calls] == ["coord", "hidden_test_author"]


def test_the_switch_is_frozen_on_the_job_when_its_spec_is_filed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec with the switch off, flip it on, build: the job still takes the disabled path."""
    w = world
    _flagged_world(w)
    job = _request(w)
    assert (
        json.loads(w.store.get_artifact(job, "spec_gate.json").content)["hidden_tests_enabled"]
        is False
    )
    w.runner.cfg.repos.repos[REPO].hidden_tests = HiddenTestsPolicy(enabled=True)  # flipped later
    _tripwires(w, monkeypatch)
    w.standin.worker_edit = _flag_edit()
    item = _spec_item(w)
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    pr = next(p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w1")
    assert pr["title"] == TITLE and pr["draft"] is True
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert "hidden" not in rec and rec["hidden_disabled"] is True
    assert w.store.get_job(job).state.value != "escalated"
