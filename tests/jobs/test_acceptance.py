"""Stage 3 acceptance, machine-checked on the host, in the vault, after the
worker exits. Test results come only from the verifier; it fails closed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from tests.harness import finish_session
from vigild.contract.models import WorkerReport
from vigild.jobs.acceptance import check_worker
from vigild.workspace import Workspace

REPO, TID, BRANCH = "o/fixture", "fx-0001-w1", "vigil/fx-0001-w1"
CONTRACT: dict[str, Any] = {
    "task_id": TID,
    "job_id": "fx-0001",
    "spec": {
        "goal": "g",
        "test_cmd": "pytest -q tests/test_feature_map.py",
        "done_when": "d",
        "must_fail_first": True,
        "files_in_scope": ["docs/feature-map.md"],
    },
    "limits": {"max_cost_usd": 1.0},
}
TESTS_OK = {"before": {"outcome": "fail"}, "after": {"outcome": "pass"}, "source": "verifier"}


def _report(**kw: object) -> WorkerReport:
    base: dict[str, object] = {
        "task_id": TID, "job_id": "fx-0001", "test_before": "fail", "test_after": "pass",
        "files_changed": ["docs/feature-map.md"], "summary": "s",
        "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "-",
                       "reviewer_would_flag": "-", "could_delete": "-"},
        "provenance": "claude",
    }  # fmt: skip
    base.update(kw)
    return WorkerReport.model_validate(base)


@pytest.fixture
def ws(tmp_path: Path, fixture_repo: dict[str, Path]) -> Workspace:
    return Workspace(tmp_path / "d", remote_url=lambda r: str(fixture_repo["bare"]))


def _result(ws: Workspace, tmp_path: Path, extra: object = None) -> tuple[str, str]:
    c = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    (c / "docs" / "feature-map.md").write_text("# Feature map\nrate limit\n")
    if callable(extra):
        extra(c)
    r = finish_session(c, tmp_path / "out", branch=BRANCH, base_sha=base)
    return base, ws.import_result(REPO, TID, tmp_path / "out", BRANCH, base, head=r["head"])


def _check(
    ws: Workspace,
    base: str,
    head: str,
    *,
    contract: dict[str, Any] = CONTRACT,
    report: WorkerReport | None = None,
    tests: Any = TESTS_OK,
    cost: float = 0.2,
) -> list[str]:
    return check_worker(ws, REPO, base, head, contract, report or _report(), tests, cost=cost)


def test_clean_worker_passes(ws: Workspace, tmp_path: Path) -> None:
    assert _check(ws, *_result(ws, tmp_path)) == []


def test_test_must_fail_first(ws: Workspace, tmp_path: Path) -> None:
    base, head = _result(ws, tmp_path)
    passed_before = {**TESTS_OK, "before": {"outcome": "pass"}}
    assert any("verifier saw" in e for e in _check(ws, base, head, tests=passed_before))


def test_test_must_pass_after(ws: Workspace, tmp_path: Path) -> None:
    base, head = _result(ws, tmp_path)
    failed = {**TESTS_OK, "after": {"outcome": "fail"}}
    assert any("must pass" in e for e in _check(ws, base, head, tests=failed))


def test_the_reports_test_fields_are_ignored(ws: Workspace, tmp_path: Path) -> None:
    """The verifier decides; what the worker says about its tests doesn't."""
    base, head = _result(ws, tmp_path)
    liar = _report(test_before="pass", test_after="fail")
    assert _check(ws, base, head, report=liar) == []
    failed = {**TESTS_OK, "after": {"outcome": "fail"}}
    assert _check(ws, base, head, report=_report(), tests=failed)


# blocker 4: the record is required, comes from the verifier, and "unknown" is no pass
@pytest.mark.parametrize(
    "tests", [None, {}, {"before": {"outcome": "fail"}, "after": {"outcome": "pass"}}]
)
def test_a_missing_verifier_record_is_a_rejection(
    ws: Workspace, tmp_path: Path, tests: Any
) -> None:
    base, head = _result(ws, tmp_path)
    assert any("record is missing" in e for e in _check(ws, base, head, tests=tests))


@pytest.mark.parametrize("which", ["before", "after"])
@pytest.mark.parametrize("what", ["unknown", "timeout"])
def test_unknown_or_timeout_is_a_rejection(
    ws: Workspace, tmp_path: Path, which: str, what: str
) -> None:
    base, head = _result(ws, tmp_path)
    assert _check(ws, base, head, tests={**TESTS_OK, which: {"outcome": what}})


@pytest.mark.parametrize("cmd", [None, "", "   "])
def test_no_test_cmd_is_a_rejection(ws: Workspace, tmp_path: Path, cmd: str | None) -> None:
    """Review round 2, N2: an empty test_cmd used to accept with no record."""
    base, head = _result(ws, tmp_path)
    contract = {**CONTRACT, "spec": {**CONTRACT["spec"], "test_cmd": cmd}}
    assert any("no test_cmd" in e for e in _check(ws, base, head, contract=contract, tests=None))


def test_files_outside_scope_fail(ws: Workspace, tmp_path: Path) -> None:
    base, head = _result(ws, tmp_path, lambda c: (c / "README.md").write_text("x\n"))
    assert any("README.md" in e for e in _check(ws, base, head))


def test_a_weakened_test_fails(
    ws: Workspace, tmp_path: Path, fixture_repo: dict[str, Path]
) -> None:
    seed = fixture_repo["seed"]
    (seed / "tests").mkdir()
    (seed / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n    assert 2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "t")
    git(seed, "push", "-q", "origin", "main")
    base, head = _result(
        ws,
        tmp_path,
        lambda c: (c / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n"),
    )
    contract = {
        **CONTRACT,
        "spec": {**CONTRACT["spec"], "files_in_scope": ["docs/feature-map.md", "tests/test_x.py"]},
    }
    assert any("weaken" in e for e in _check(ws, base, head, contract=contract))


def test_over_the_cost_limit_fails(ws: Workspace, tmp_path: Path) -> None:
    assert any("cost" in e for e in _check(ws, *_result(ws, tmp_path), cost=1.5))


def test_report_must_match_the_task(ws: Workspace, tmp_path: Path) -> None:
    base, head = _result(ws, tmp_path)
    assert any("task_id" in e for e in _check(ws, base, head, report=_report(task_id="other")))


# --- review round 3, F1 and F3 ----------------------------------------------------------


def _with(contract: dict[str, Any], scope: list[str]) -> dict[str, Any]:
    return {**contract, "spec": {**contract["spec"], "files_in_scope": scope}}


def test_a_runner_config_change_is_rejected_unless_named(ws: Workspace, tmp_path: Path) -> None:
    from vigild.jobs.acceptance import runner_config_changes

    base, head = _result(ws, tmp_path, lambda c: (c / "README.md").write_text("x\n"))
    cfg = ["README.md"]  # stands in for package.json
    # a glob that covers it is not enough: the spec must name the exact path
    wide = _with(CONTRACT, ["docs/feature-map.md", "*.md"])
    errors = check_worker(ws, REPO, base, head, wide, _report(), TESTS_OK, cost=0.1,
                          runner_config=cfg)  # fmt: skip
    assert any("changes how tests run" in e and "README.md" in e for e in errors), errors
    named = _with(CONTRACT, ["docs/feature-map.md", "README.md"])
    assert check_worker(ws, REPO, base, head, named, _report(), TESTS_OK, cost=0.1,
                        runner_config=cfg) == []  # fmt: skip
    refused, allowed = runner_config_changes(ws, REPO, base, head, named, cfg)
    assert refused == [] and allowed == ["README.md"]


def test_a_script_test_cmd_runs_is_runner_config(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        (c / "run_tests.sh").write_text("exit 0\n")

    base, head = _result(ws, tmp_path, edit)
    contract = _with(CONTRACT, ["docs/feature-map.md", "*.sh"])
    contract["spec"]["test_cmd"] = "sh run_tests.sh"
    errors = check_worker(ws, REPO, base, head, contract, _report(), TESTS_OK, cost=0.1)
    assert any("run_tests.sh" in e and "changes how tests run" in e for e in errors), errors


@pytest.mark.parametrize(
    ("ids", "want"),
    [
        ({"checked": True, "failing_before": [], "not_passing_after": []}, "none of"),
        ({"checked": True, "failing_before": ["t::a"], "not_passing_after": ["t::a"]}, "t::a"),
    ],
)
def test_test_ids_decide_must_fail_first_when_checked(
    ws: Workspace, tmp_path: Path, ids: dict[str, Any], want: str
) -> None:
    base, head = _result(ws, tmp_path)
    errors = _check(ws, base, head, tests={**TESTS_OK, "ids": ids})
    assert any(want in e for e in errors), errors
    ok = {"checked": True, "failing_before": ["t::a"], "not_passing_after": []}
    assert _check(ws, base, head, tests={**TESTS_OK, "ids": ok}) == []


@pytest.mark.parametrize(
    "path",
    [
        "server/test/zoneNumber.test.ts",  # ParkAgent's server and executor
        "scripts/audit.test.mjs",
        "data/test_import_parkboston_zones.py",
        "server/test/pg/cas.pg.ts",
        "web/src/button.spec.ts",  # round 3, F4
        "web/src/__tests__/button.ts",
    ],
)
def test_test_files_are_recognised(path: str) -> None:
    from vigild.verifier import TEST_FILE

    assert TEST_FILE.search(path), path


def test_source_files_are_not_tests() -> None:
    from vigild.verifier import TEST_FILE

    for path in ("server/src/app.ts", "src/testing_utils.py", "docs/test-plan.md"):
        assert not TEST_FILE.search(path), path


def test_committed_node_modules_are_rejected_outright(ws: Workspace, tmp_path: Path) -> None:
    """Round 4, item 5: a committed node_modules/.bin/vitest would run instead
    of the real one; nothing under node_modules is ever accepted, named or not."""

    def edit(c: Path) -> None:
        (c / "node_modules" / ".bin").mkdir(parents=True)
        (c / "node_modules" / ".bin" / "vitest").write_text("#!/bin/sh\nexit 0\n")

    base, head = _result(ws, tmp_path, edit)
    named = _with(CONTRACT, ["docs/feature-map.md", "node_modules/.bin/vitest"])
    errors = check_worker(ws, REPO, base, head, named, _report(), TESTS_OK, cost=0.1)
    assert any("node_modules" in e and "never" in e for e in errors), errors


def test_repo_runner_config_covers_the_round4_gaps() -> None:
    from vigild import config
    from vigild.intake.paths import in_scope

    cfg = config.load()
    vigil = cfg.repo("thomasbardhi01/vigil").runner_config
    for f in ("pytest.toml", ".pytest.toml", ".pytest.ini", "uv.toml", ".python-version"):
        assert in_scope(f, vigil), f
    pa = cfg.repo("thomasbardhi01/parkagent").runner_config
    for f in (
        ".npmrc",
        "server/.npmrc",
        "node_modules/.bin/vitest",
        "server/node_modules/vitest/index.js",
        "server/test/setup/reasonsGate.ts",
    ):
        assert in_scope(f, pa), f


# --- phase 1 run, finding #2: a runner-file task passes only on its hidden tests -------------

FLAGGED = {**TESTS_OK, "runner_files": ["conftest.py"], "runner_file_changed": True}
HIDDEN_OK = {"ok": True, "files": 1, "failed_on_base": True, "passed_on_head": True}


def test_an_unflagged_task_needs_no_hidden_tests(ws: Workspace, tmp_path: Path) -> None:
    base, head = _result(ws, tmp_path)
    assert _check(ws, base, head, tests={**TESTS_OK, "runner_files": []}) == []


def test_visible_tests_are_not_enough_for_a_runner_file_task(ws: Workspace, tmp_path: Path) -> None:
    from vigild.jobs.acceptance import escalates

    base, head = _result(ws, tmp_path)
    errors = _check(ws, base, head, tests=FLAGGED)  # visible: fail then pass; no hidden record
    assert any("no hidden tests" in e for e in errors), errors
    assert escalates(FLAGGED)
    absent = {**FLAGGED, "hidden": {"present": False, "reason": "no session"}}
    assert escalates(absent) and _check(ws, base, head, tests=absent)


def test_hidden_tests_that_pass_let_a_runner_file_task_pass(ws: Workspace, tmp_path: Path) -> None:
    from vigild.jobs.acceptance import escalates

    base, head = _result(ws, tmp_path)
    ok = {**FLAGGED, "hidden": HIDDEN_OK}
    assert _check(ws, base, head, tests=ok) == []
    assert not escalates(ok)


@pytest.mark.parametrize(
    ("hidden", "want"),
    [
        ({**HIDDEN_OK, "passed_on_head": False, "ok": False}, "pass on the head"),
        ({**HIDDEN_OK, "failed_on_base": False, "ok": False}, "fail on the base"),
        ({**HIDDEN_OK, "tampered": ["x"], "ok": False}, "hidden directory"),
        ({**HIDDEN_OK, "ran": {"checked": True, "ok": False}, "ok": False}, "head ran less"),
    ],
)
def test_failing_hidden_tests_reject_a_task_whose_visible_tests_pass(
    ws: Workspace, tmp_path: Path, hidden: dict[str, Any], want: str
) -> None:
    from vigild.jobs.acceptance import escalates

    base, head = _result(ws, tmp_path)
    errors = _check(ws, base, head, tests={**FLAGGED, "hidden": hidden})
    assert any(want in e for e in errors), errors
    assert not escalates({**FLAGGED, "hidden": hidden})  # they exist; the task just fails
    # and the failure names no hidden test: it can reach the coord's replan context
    assert "test_hidden" not in " ".join(errors)
