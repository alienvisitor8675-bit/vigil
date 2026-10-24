"""The verifier (owner direction, review rounds 2 and 3): test results come only
from fresh checkouts vigild makes from the vault, with the base's runner files,
run in containers it owns. Here a local runner (bash with pipefail and errexit,
pytest asked for a JUnit report) stands in for the container;
tests/security/test_verifier_docker.py runs the real one."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from tests.harness import LocalTestRunner, finish_session
from vigild.verifier import Verifier, judge, outcome, with_reports
from vigild.workspace import Workspace

KEY = b"k" * 32
REPO = "o/fixture"
TID = "fx-0001-w1"
BRANCH = f"vigil/{TID}"
CMD = "pytest -q tests"
FEATURE_TEST = "def test_flag():\n    from src.feature import flag\n\n    assert flag() is True\n"


@pytest.fixture
def ws(tmp_path: Path, fixture_repo: dict[str, Path]) -> Workspace:
    """A small Python repo: one passing test, one feature the worker turns on."""
    seed = fixture_repo["seed"]
    files = {
        "conftest.py": "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n",
        "src/__init__.py": "",
        "src/feature.py": "def flag():\n    return False\n",
        "tests/test_old.py": "def test_old():\n    assert 1 + 1 == 2\n\n\ndef test_old_2():\n    assert True\n",
        "run_tests.sh": "pytest -q tests\n",
        "package.json": '{"scripts": {"test": "pytest -q tests"}}\n',
    }
    for name, text in files.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "a python fixture")
    git(seed, "push", "-q", "origin", "main")
    return Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))


def _session(ws: Workspace, tmp_path: Path, edit: Callable[[Path], Any]) -> tuple[str, str]:
    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    edit(clone)
    out = tmp_path / "out"
    r = finish_session(clone, out, branch=BRANCH, base_sha=base)
    return base, ws.import_result(REPO, TID, out, BRANCH, base, head=r["head"])


def _verify(
    ws: Workspace,
    base: str,
    head: str,
    cmd: str = CMD,
    *,
    mff: bool = True,
    config: list[str] | None = None,
    runner: LocalTestRunner | None = None,
) -> dict[str, Any]:
    return Verifier(ws, runner or LocalTestRunner(), host_key=KEY).verify(
        repo=REPO,
        image="img",
        task_id=TID,
        base_sha=base,
        head=head,
        test_cmd=cmd,
        must_fail_first=mff,
        runner_config=["conftest.py", "package.json"] if config is None else config,
    )


def _turn_on(c: Path) -> None:
    (c / "tests" / "test_flag.py").write_text(FEATURE_TEST)
    (c / "src" / "feature.py").write_text("def flag():\n    return True\n")


# --- the three runs --------------------------------------------------------------------------


def test_a_real_red_green_passes_every_check(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify(ws, base, head)
    assert rec["base"]["sha"] == base and rec["base"]["tests"] == 2
    assert rec["before"]["sha"] == base and rec["before"]["outcome"] == "fail"
    assert rec["before"]["applied"] == ["tests/test_flag.py"]
    assert rec["after"]["sha"] == head and rec["after"]["outcome"] == "pass"
    assert rec["ids"] == {
        "checked": True,
        "failing_before": ["tests.test_flag::test_flag"],
        "not_passing_after": [],
    }
    assert rec["ran"]["ok"] and rec["ran"]["ids"] == {"base": 2, "head": 3}
    assert rec["provenance"] == "human" and rec["source"] == "verifier"


def test_must_fail_first_off_runs_base_and_after(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _turn_on)
    runner = LocalTestRunner()
    rec = _verify(ws, base, head, mff=False, runner=runner)
    assert "before" not in rec and len(runner.calls) == 3  # base twice, after


def test_each_run_gets_a_fresh_checkout_with_no_hooks(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _turn_on)
    runner = LocalTestRunner()
    _verify(ws, base, head, runner=runner)
    sources = [call["source"] for call in runner.calls]
    assert len(set(sources)) == 4  # base twice, before, after
    for seen in runner.calls:
        assert seen["hooksPath"] == "/dev/null"
        assert not seen["worker_files"], seen
    assert not any(Path(s).exists() for s in sources), "checkouts are removed after the run"


def test_the_hook_forgery_and_a_fifo_record_change_nothing(ws: Workspace, tmp_path: Path) -> None:
    """The reviewer's forgery (round 2): commondir pointing at a copy with
    core.fsmonitor and hooks/post-commit that rewrite the old record path, plus
    a FIFO planted at that path. The verdict comes from the vault checkout;
    neither runs or is read, and the test still fails: the work doesn't pass it."""
    marker = tmp_path / "PWNED"
    out = tmp_path / "out"

    def edit(c: Path) -> None:
        evil = tmp_path / "evil-common"
        (evil / "hooks").mkdir(parents=True)
        script = f"#!/bin/sh\necho ran >> {marker}\necho '{{}}' > {out}/harness_tests.json\n"
        for name in ("post-commit", "pre-commit", "post-checkout"):
            (evil / "hooks" / name).write_text(script)
            (evil / "hooks" / name).chmod(0o755)
        fsm = tmp_path / "fsm.sh"
        fsm.write_text(script)
        fsm.chmod(0o755)
        (evil / "config").write_text(
            f"[core]\n\thooksPath = {evil / 'hooks'}\n\tfsmonitor = {fsm}\n"
        )
        for d in ("objects", "refs"):
            os.symlink(c / ".git" / d, evil / d)
        (c / ".git" / "commondir").write_text(str(evil) + "\n")
        (c / "tests" / "test_flag.py").write_text(FEATURE_TEST)  # the code isn't changed
        out.mkdir(parents=True, exist_ok=True)
        os.mkfifo(out / "harness_tests.json")

    clone = ws.clone(REPO, TID, branch=BRANCH, base="main")
    base = ws.base_sha(TID)
    edit(clone)
    # the stand-in commits with git pinned, as the real harness does; what's
    # tested is vigild's side
    pins = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    env = {**os.environ, "GIT_COMMON_DIR": str(clone / ".git")}
    for args in (["add", "-A"], ["commit", "-qm", "w"]):
        subprocess.run(["git", *pins, *args], cwd=clone, check=True, env=env)
    bundle = ["bundle", "create", "-q", str(out / "result.bundle"), f"refs/heads/{BRANCH}"]
    git(clone, *bundle, f"^{base}")
    head = ws.import_result(REPO, TID, out, BRANCH, base, head=git(clone, "rev-parse", BRANCH))
    rec = _verify(ws, base, head)
    assert rec["after"]["outcome"] == "fail"
    assert not marker.exists()
    assert (out / "harness_tests.json").is_fifo()  # never opened, never replaced


# --- round 3, F1: the base's runner files in every run ----------------------------------------


def test_a_rewritten_test_script_runs_as_the_base_wrote_it(ws: Workspace, tmp_path: Path) -> None:
    """The reviewer's replay: `sh run_tests.sh`, rewritten to `exit 0`."""

    def edit(c: Path) -> None:
        (c / "tests" / "test_flag.py").write_text(FEATURE_TEST)
        (c / "run_tests.sh").write_text("exit 0\n")

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head, "sh run_tests.sh")
    assert rec["after"]["outcome"] == "fail"  # the base's script ran the real tests
    assert rec["runner_config"]["pinned_to_base"] == ["run_tests.sh"]


def test_a_narrowed_package_script_runs_as_the_base_wrote_it(ws: Workspace, tmp_path: Path) -> None:
    """The ParkAgent version: package.json's test script narrowed to the new
    test would hide a regression in the old ones."""

    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").unlink()  # pretend it would fail now
        (c / "package.json").write_text('{"scripts": {"test": "pytest -q tests/test_flag.py"}}\n')

    base, head = _session(ws, tmp_path, edit)
    # stands in for `pnpm test`: run package.json's test script
    cmd = (
        'python3 -c "import json,subprocess,sys;'
        "sys.exit(subprocess.call(json.load(open('package.json'))['scripts']['test'],shell=True))\""
    )
    rec = _verify(ws, base, head, cmd)
    assert "package.json" in rec["runner_config"]["pinned_to_base"]
    assert not rec["ran"]["ok"]  # the base's script runs every test: head ran fewer
    assert rec["ran"]["missing_files"] == ["tests.test_old"]


def test_runner_files_come_from_the_base_even_when_the_spec_names_them(
    ws: Workspace, tmp_path: Path
) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "conftest.py").write_text("raise SystemExit('the worker\\'s conftest ran')\n")

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head)
    assert rec["runner_config"]["pinned_to_base"] == ["conftest.py"]
    assert rec["after"]["outcome"] == "pass"


# --- round 3: what ran, base against head -----------------------------------------------------


def test_an_added_skip_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").write_text(
            "import pytest\n\n\n@pytest.mark.skip\ndef test_old():\n    assert 1 + 1 == 2\n\n\n"
            "def test_old_2():\n    assert True\n"
        )

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head)
    assert rec["after"]["outcome"] == "pass"
    assert not rec["ran"]["ok"] and rec["ran"]["skipped"] == {"base": 0, "head": 1}


def test_an_added_xfail_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").write_text(
            "import pytest\n\n\n@pytest.mark.xfail\ndef test_old():\n    assert 1 + 1 == 3\n\n\n"
            "def test_old_2():\n    assert True\n"
        )

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head)
    assert not rec["ran"]["ok"] and rec["ran"]["xfail"] == {"base": 0, "head": 1}


def test_a_deleted_test_file_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").unlink()

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head)
    assert not rec["ran"]["ok"]
    assert rec["ran"]["missing_ids"] == ["tests.test_old::test_old", "tests.test_old::test_old_2"]


# --- round 3, F3: what a result means ---------------------------------------------------------


def test_an_unrelated_failure_at_before_is_not_must_fail_first(
    ws: Workspace, tmp_path: Path
) -> None:
    """The reviewer's assert-nothing replay: the new test asserts nothing, and
    "before" fails only because of another part of test_cmd."""

    def edit(c: Path) -> None:
        (c / "tests" / "test_flag.py").write_text("def test_flag():\n    pass\n")
        (c / "docs" / "feature-map.md").write_text("rate limit\n")

    base, head = _session(ws, tmp_path, edit)
    cmd = "pytest -q tests && grep -q 'rate limit' docs/feature-map.md"
    rec = _verify(ws, base, head, cmd)
    assert rec["before"]["outcome"] == "fail" and rec["after"]["outcome"] == "pass"
    assert rec["ids"]["checked"] is True and rec["ids"]["failing_before"] == []


def test_a_collection_error_at_before_is_unknown(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        (c / "tests" / "test_new.py").write_text(
            "from src.newmod import thing\n\n\ndef test_thing():\n    assert thing() == 1\n"
        )
        (c / "src" / "newmod.py").write_text("def thing():\n    return 1\n")

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head, "pytest -q tests/test_new.py")
    assert rec["before"]["outcome"] == "unknown"  # pytest exit 2: collection error
    assert rec["after"]["outcome"] == "pass"


def test_zero_tests_through_a_pipe_is_unknown_at_head(ws: Workspace, tmp_path: Path) -> None:
    """`pnpm test 2>&1 | tail` that runs no test: pipefail carries the
    runner's exit, and "no tests" is not a pass."""
    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify(ws, base, head, "pytest -q -k no_such_test tests 2>&1 | tail -n 5", mff=False)
    assert rec["after"]["outcome"] == "unknown"


def test_a_runner_with_no_report_is_unknown_at_head(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        (c / "docs" / "feature-map.md").write_text("| rate limit | 10 |\n")

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head, "grep -q 'rate limit' docs/feature-map.md")
    assert rec["after"]["outcome"] == "unknown" and "no test report" in rec["after"]["note"]
    assert rec["ids"] == {
        "checked": False,
        "note": "test ids not checked: the runner gave no report",
    }
    assert rec["ran"]["checked"] is False


def test_a_vitest_package_script_is_asked_for_a_report() -> None:
    pkg = {"server/package.json": '{"scripts": {"test": "vitest run"}}'}
    got = with_reports("pnpm -C server test 2>&1 | tail -n 50", pkg.get)
    # pnpm reads --reporter as its own option, so the base's script runs
    # through pnpm exec with the flags added
    assert got == (
        "pnpm -C server exec bash -o pipefail -o errexit -c 'vitest run --reporter=default"
        " --reporter=junit --outputFile.junit=/tmp/vigil-report/vitest-1.xml' | tail -n 50"
    )
    pkg2 = {"package.json": '{"scripts": {"test": "prisma generate && vitest run"}}'}
    assert "prisma generate && vitest run --reporter" in with_reports("pnpm test", pkg2.get)
    pkg3 = {"package.json": '{"scripts": {"test": "vitest run; echo done"}}'}
    assert with_reports("pnpm test", pkg3.get) == "pnpm test"  # vitest isn't last: no flags
    assert with_reports("pnpm -C server lint", pkg.get) == "pnpm -C server lint"
    assert "--reporter=junit" in with_reports("npx vitest run test/a.test.ts")


@pytest.mark.parametrize(
    ("code", "timed_out", "want"),
    [
        (0, False, "pass"),
        (1, False, "fail"),
        (125, False, "unknown"),
        (127, False, "unknown"),
        (137, False, "unknown"),
        (None, True, "timeout"),
    ],
)
def test_outcomes(code: int | None, timed_out: bool, want: str) -> None:
    assert outcome(code, timed_out) == want


@pytest.mark.parametrize(
    ("cmd", "code", "output", "want"),
    [
        ("pytest -q", 2, "", "unknown"),
        ("pytest -q", 3, "", "unknown"),
        ("pytest -q", 4, "", "unknown"),
        ("pytest -q", 5, "", "unknown"),
        ("pytest -q", 1, "", "fail"),
        ("uv run pytest -q", 5, "", "unknown"),
        ("pnpm test", 1, "No test files found, exiting with code 1", "unknown"),
        ("pnpm test", 137, "", "unknown"),
        ("pnpm test", 139, "", "unknown"),
        ("grep -q x a", 2, "", "fail"),
    ],
)
def test_runner_exits_that_are_not_test_results(
    cmd: str, code: int, output: str, want: str
) -> None:
    assert judge(cmd, code, False, output) == want


# --- review round 4, item 1: a report with failures is a fail, whatever the exit ----------


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q tests && echo ok; echo done",
        "! pytest -q tests",
        "if pytest -q tests; then :; fi",
        "set +e; pytest -q tests; true",
        'trap "exit 0" EXIT; pytest -q tests',
    ],
)
def test_errexit_exempt_forms_cannot_hide_a_failing_test(
    ws: Workspace, tmp_path: Path, cmd: str
) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").write_text(
            "def test_old():\n    assert 1 + 1 == 3\n\n\ndef test_old_2():\n    assert True\n"
        )  # a regression in an existing test

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head, cmd, mff=False)
    assert rec["after"]["outcome"] == "fail", rec["after"]


@pytest.mark.parametrize(
    ("cmd", "scripts"),
    [
        ("uv run --offline pytest -q tests/test_new.py", []),  # the reviewer's example
        ("pytest -q tests/test_new.py", []),
        ("python -m pytest tests/test_a.py", []),
        ("sh run_tests.sh", ["run_tests.sh"]),
        ("bash -e scripts/check.sh", ["scripts/check.sh"]),
        ("python tools/check.py --strict", ["tools/check.py"]),
        ("node scripts/check.mjs", ["scripts/check.mjs"]),
        ("./scripts/check-city-neutral.sh", ["scripts/check-city-neutral.sh"]),
        ("uv run tools/check.py", ["tools/check.py"]),
        ("uv run python tools/check.py", ["tools/check.py"]),
        ("cd server && pnpm test", []),
    ],
)
def test_only_programs_and_interpreter_scripts_are_invoked_scripts(
    cmd: str, scripts: list[str]
) -> None:  # round 4, item 2
    from vigild.verifier import invoked_scripts

    assert invoked_scripts(cmd) == scripts


def test_a_new_test_named_on_the_command_line_is_not_pinned_away(
    ws: Workspace, tmp_path: Path
) -> None:
    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify(ws, base, head, "pytest -q tests/test_flag.py")
    assert rec["runner_config"]["pinned_to_base"] == []
    assert rec["after"]["outcome"] == "pass" and rec["before"]["outcome"] == "fail"


# --- review round 4, item 4: a base run with no report doesn't fail open -------------------


def test_with_no_base_report_head_is_compared_with_before(ws: Workspace, tmp_path: Path) -> None:
    """The reviewer's case: a check that fails at base before the runner starts
    (here: the new test file is missing), so base reports nothing. Head is
    compared with "before" minus the worker's added tests, so a deleted old
    test is still caught."""

    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").unlink()  # deleted tests aren't overlaid at before

    base, head = _session(ws, tmp_path, edit)
    rec = _verify(ws, base, head, "test -f tests/test_flag.py && pytest -q tests")
    assert rec["base"]["tests"] == 0 and rec["before"]["tests"] == 3
    assert rec["ran"]["against"] == "before" and not rec["ran"]["ok"]
    assert rec["ran"]["missing_ids"] == ["tests.test_old::test_old", "tests.test_old::test_old_2"]


def test_with_no_report_at_base_or_before_what_ran_is_unknown(
    ws: Workspace, tmp_path: Path
) -> None:
    from vigild.jobs.acceptance import check_tests

    base, head = _session(ws, tmp_path, _turn_on)
    # a "lint" that fails at base and before (the code isn't there yet), so
    # pytest never runs there
    lint = "python3 -c 'import sys; sys.path.insert(0, \".\"); import src.feature as f; assert f.flag()'"
    rec = _verify(ws, base, head, f"{lint} && pytest -q tests")
    assert rec["after"]["outcome"] == "pass"
    assert rec["ran"] == {
        "checked": False,
        "unknown": True,
        "note": "neither base nor before produced a report: what ran can't be compared",
    }
    errors = check_tests({"test_cmd": "x", "must_fail_first": True}, rec)
    assert any("can't be compared" in e for e in errors), errors


# --- review round 4, item 5: runner-config gaps --------------------------------------------


def test_a_root_pytest_toml_cannot_change_how_tests_run(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _turn_on(c)
        (c / "tests" / "test_old.py").write_text(
            "def test_old():\n    assert 1 + 1 == 3\n\n\ndef test_old_2():\n    assert True\n"
        )
        (c / "pytest.toml").write_text(
            '[pytest]\naddopts = ["--deselect", "tests/test_old.py::test_old"]\n'
        )

    base, head = _session(ws, tmp_path, edit)
    vigil = __import__("vigild.config", fromlist=["load"]).load().repo("thomasbardhi01/vigil")
    rec = _verify(ws, base, head, config=list(vigil.runner_config))
    assert "pytest.toml" in rec["runner_config"]["pinned_to_base"]
    assert rec["after"]["outcome"] == "fail"  # the regression is still run


# --- review round 4, item 6: one resolver for the gate and the verifier ------------------------

PKG = {
    "server/package.json": '{"scripts": {"test": "vitest run", "lint": "eslint src"}}',
    "package.json": '{"scripts": {"test": "vitest run"}}',
}


@pytest.mark.parametrize(
    ("cmd", "want"),
    [
        (
            "cd server && pnpm test",
            "cd server && pnpm exec bash -o pipefail -o errexit -c 'vitest run --reporter=default"
            " --reporter=junit --outputFile.junit=/tmp/vigil-report/vitest-1.xml'",
        ),
        (
            "pnpm -C server test test/x.test.ts",  # arguments pass through
            "pnpm -C server exec bash -o pipefail -o errexit -c 'vitest run test/x.test.ts"
            " --reporter=default --reporter=junit --outputFile.junit=/tmp/vigil-report/vitest-1.xml'",
        ),
        (
            "pnpm -C server run test",
            "pnpm -C server exec bash -o pipefail -o errexit -c 'vitest run --reporter=default"
            " --reporter=junit --outputFile.junit=/tmp/vigil-report/vitest-1.xml'",
        ),
        ("pytest -q tests", "pytest -q tests"),
    ],
)
def test_the_resolver_attaches_a_report_and_keeps_arguments(cmd: str, want: str) -> None:
    from vigild.verifier import plan_reports

    plan = plan_reports(cmd, PKG.get)
    assert plan.problems == [] and plan.command == want


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("pnpm --filter server test", "--filter"),
        ("pnpm -C server run lint", "runs no test runner"),
        ("pnpm -C server lint && grep -q x a", "runs no test runner"),
        ("npm test", "npm"),
        ("grep -q x docs/a.md", "runs no test runner"),
    ],
)
def test_the_resolver_refuses_what_it_cant_attach_a_report_to(cmd: str, why: str) -> None:
    from vigild.verifier import plan_reports

    plan = plan_reports(cmd, PKG.get)
    assert any(why in p for p in plan.problems), plan.problems


def test_the_gate_uses_the_same_resolver() -> None:
    from vigild.intake import spec_gate

    test_cmd_problem = spec_gate.test_cmd_problem
    assert test_cmd_problem("pnpm -C server run lint", PKG.get)
    assert test_cmd_problem("pnpm --filter server test", PKG.get)
    assert test_cmd_problem("cd server && pnpm test", PKG.get) is None


@pytest.mark.parametrize(
    "classname",
    ["tests.test_a", "tests.test_a.TestX", "tests.test_a.TestX.TestNested", "test/a.test.ts"],
)
def test_a_case_belongs_to_its_file_class_based_or_not(classname: str) -> None:  # round 4, item 9
    from vigild.verifier import Case, belongs

    files = ["tests/test_a.py", "server/test/a.test.ts"]
    assert belongs(Case(f"{classname}::t", classname, "pass"), files)
    assert not belongs(Case("tests.test_b.TestX::t", "tests.test_b.TestX", "pass"), files)


# --- phase 1 run, finding #2: a task whose scope names a runner file -------------------------

FLAG_TEST = "import os\n\n\ndef test_flag_env():\n    assert os.environ.get('FLAG_ON') == '1'\n"
TOOL_OK = 'print("ok")\n'
# black-box: the subject is a command, run in another process on a read-only checkout
HIDDEN_FLAG = (
    "from vigil_hidden import check, run_subject\n\n\n"
    "def test_tool_says_ok():\n"
    "    r = run_subject(['python3', 'tools/check.py'])\n"
    "    check(r['returncode'] == 0 and r['stdout'].strip() == 'ok', repr(r))\n"
)
FLAG_CONFTEST = (
    "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\nos.environ['FLAG_ON'] = '1'\n"
)
HIDDEN_DIR = ".vigil/fx-0001/hidden/fx-0001-w1"


def _runner_edit(c: Path) -> None:
    """The worker changes a runner file the spec names (conftest.py) and one it
    doesn't (package.json), with its own test."""
    (c / "tests" / "test_flag.py").write_text(FLAG_TEST)
    (c / "conftest.py").write_text(FLAG_CONFTEST)
    (c / "tools").mkdir(exist_ok=True)
    (c / "tools" / "check.py").write_text(TOOL_OK)
    (c / "package.json").write_text('{"scripts": {"test": "true"}}\n')


def _hidden_suite(ws: Workspace, tmp_path: Path, body: str = HIDDEN_FLAG, expected: int = 1) -> Any:
    """What the hidden-test session leaves in the vault: a commit on a branch
    that is never pushed."""
    from vigild.verifier import HiddenSuite

    def edit(c: Path) -> None:
        (c / HIDDEN_DIR).mkdir(parents=True)
        (c / HIDDEN_DIR / "test_hidden.py").write_text(body)

    clone = ws.clone(REPO, "fx-0001-ht1", branch="vigil/fx-0001-hidden", base="main")
    base = ws.base_sha("fx-0001-ht1")
    edit(clone)
    out = tmp_path / "hidden-out"
    r = finish_session(clone, out, branch="vigil/fx-0001-hidden", base_sha=base)
    sha = ws.import_result(REPO, "fx-0001-ht1", out, "vigil/fx-0001-hidden", base, head=r["head"])
    return HiddenSuite(
        sha=sha,
        prefix=HIDDEN_DIR,
        files=(f"{HIDDEN_DIR}/test_hidden.py",),
        test_cmd=f"pytest -q {HIDDEN_DIR}/test_hidden.py",
        expected=expected,
    )


def _verify_flagged(
    ws: Workspace,
    base: str,
    head: str,
    hidden: Any = None,
    runner: LocalTestRunner | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    return Verifier(ws, runner or LocalTestRunner(), host_key=KEY).verify(
        repo=REPO,
        image="img",
        task_id=TID,
        base_sha=base,
        head=head,
        test_cmd=CMD,
        must_fail_first=True,
        runner_config=["conftest.py", "package.json", "tools/check.py"],
        runner_overlap=["conftest.py", "tools/check.py"],
        hidden=hidden,
        scope=scope,
    )


def test_overlap_is_the_literal_scope_entries_the_verifier_pins() -> None:
    from vigild.verifier import runner_overlap

    cfg = ["conftest.py", "package.json", "scripts/check.mjs"]
    scope = ["src/a.py", "scripts/check.mjs", "package.json", "tests/*.py", "docs/**"]
    assert runner_overlap(scope, cfg, "pytest -q tests") == ["package.json", "scripts/check.mjs"]
    # a script test_cmd runs as its program is pinned too, so it counts
    assert runner_overlap(["run_tests.sh", "src/a.py"], [], "sh run_tests.sh") == ["run_tests.sh"]
    # the same strict matcher the verifier pins with
    assert runner_overlap(["sub/dir/conftest.py"], ["conftest.py"], "pytest") == []
    assert runner_overlap(["sub/dir/conftest.py"], ["**/conftest.py"], "pytest") == [
        "sub/dir/conftest.py"
    ]
    assert runner_overlap(["src/a.py"], cfg, "pytest -q tests") == []


def test_a_flagged_task_runs_the_head_copy_of_only_the_overlapping_file(
    ws: Workspace, tmp_path: Path
) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    # control: unflagged, conftest.py is pinned to the base, so the worker's test can't pass
    plain = _verify(ws, base, head)
    assert plain["after"]["outcome"] == "fail"
    assert plain["runner_config"]["pinned_to_base"] == ["conftest.py", "package.json"]

    runner = LocalTestRunner()
    rec = _verify_flagged(ws, base, head, runner=runner)
    assert rec["after"]["outcome"] == "pass"  # head's conftest.py ran
    assert rec["before"]["outcome"] == "fail"  # base's conftest.py + the worker's test
    assert rec["runner_config"]["pinned_to_base"] == ["package.json"]  # every other one stays
    assert rec["runner_file_changed"] is True
    assert rec["runner_files"] == ["conftest.py", "tools/check.py"]
    assert "hidden" not in rec  # none was given


def test_a_flagged_task_records_the_class_even_if_the_file_is_unchanged(
    ws: Workspace, tmp_path: Path
) -> None:
    base, head = _session(ws, tmp_path, _turn_on)
    rec = _verify_flagged(ws, base, head)
    assert (
        rec["runner_files"] == ["conftest.py", "tools/check.py"]
        and rec["runner_file_changed"] is False
    )
    plain = _verify(ws, base, head)
    assert plain["runner_files"] == [] and plain["runner_file_changed"] is False


def test_hidden_tests_run_on_base_and_head_as_their_own_run(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    suite = _hidden_suite(ws, tmp_path)
    runner = LocalTestRunner()
    rec = _verify_flagged(ws, base, head, suite, runner)
    h = rec["hidden"]
    assert h["ok"] and h["failed_on_base"] and h["passed_on_head"]
    assert h["base"]["sha"] == base and h["head"]["sha"] == head
    assert h["base"]["outcome"] == "fail" and h["head"]["outcome"] == "pass"
    assert h["ran"]["ok"]
    # the visible test_cmd runs and its checks are unchanged
    assert rec["after"]["outcome"] == "pass" and rec["ran"]["ok"]
    hidden_calls = [c for c in runner.calls if "test_hidden.py" in c["command"]]
    assert len(hidden_calls) == 2


def test_hidden_tests_that_head_fails_decide_against_a_passing_visible_test(
    ws: Workspace, tmp_path: Path
) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    suite = _hidden_suite(
        ws, tmp_path, "from vigil_hidden import check\n\n\ndef test_hidden():\n    check(False)\n"
    )
    rec = _verify_flagged(ws, base, head, suite)
    assert rec["after"]["outcome"] == "pass"  # the visible test is fine
    assert rec["hidden"]["ok"] is False and rec["hidden"]["passed_on_head"] is False


def test_hidden_tests_that_pass_on_base_are_not_ok(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    suite = _hidden_suite(ws, tmp_path, "def test_hidden():\n    assert True\n")
    rec = _verify_flagged(ws, base, head, suite)
    assert rec["hidden"]["failed_on_base"] is False and rec["hidden"]["ok"] is False


def test_a_worker_cannot_plant_files_in_the_hidden_directory(ws: Workspace, tmp_path: Path) -> None:
    def edit(c: Path) -> None:
        _runner_edit(c)
        (c / HIDDEN_DIR).mkdir(parents=True)
        (c / HIDDEN_DIR / "conftest.py").write_text("collect_ignore_glob = ['*']\n")

    base, head = _session(ws, tmp_path, edit)
    suite = _hidden_suite(ws, tmp_path)
    runner = LocalTestRunner()
    rec = _verify_flagged(ws, base, head, suite, runner)
    assert rec["hidden"]["ok"] is False
    assert rec["hidden"]["tampered"] == [f"{HIDDEN_DIR}/conftest.py"]


def test_hidden_tests_are_checked_on_base_alone_at_spec_time(ws: Workspace, tmp_path: Path) -> None:
    suite = _hidden_suite(ws, tmp_path)
    base = ws.base_sha("fx-0001-ht1")
    v = Verifier(ws, LocalTestRunner(), host_key=KEY)
    got = v.check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=suite,
        runner_config=["conftest.py", "tools/check.py"],
        overlap=["conftest.py", "tools/check.py"],
    )  # fmt: skip
    assert got["ok"] and got["outcome"] == "fail" and got["failing"] >= 1
    green = _hidden_suite(ws, tmp_path, "def test_hidden():\n    assert True\n")
    got = v.check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=green,
        runner_config=["conftest.py", "tools/check.py"],
        overlap=["conftest.py", "tools/check.py"],
    )  # fmt: skip
    assert not got["ok"] and "pass" in got["why"]


def test_a_hidden_test_that_errors_instead_of_failing_an_assertion_is_the_wrong_way(
    ws: Workspace, tmp_path: Path
) -> None:
    """An import or collection error is a JUnit <error>, not a <failure>."""
    suite = _hidden_suite(
        ws, tmp_path, "import no_such_module_anywhere\n\n\ndef test_h():\n    pass\n"
    )
    base = ws.base_sha("fx-0001-ht1")
    got = Verifier(ws, LocalTestRunner(), host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=suite,
        runner_config=["conftest.py", "tools/check.py"],
        overlap=["conftest.py", "tools/check.py"],
    )  # fmt: skip
    assert not got["ok"] and "errored" in got["why"] and "assertion" in got["why"]


def test_one_erroring_test_among_failing_ones_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    body = (
        "import pytest\n\n\n@pytest.fixture\ndef boom():\n    raise RuntimeError('setup')\n\n\n"
        "from vigil_hidden import check\n\n\ndef test_a():\n    check(False)\n\n\ndef test_b(boom):\n    check(False)\n"
    )
    suite = _hidden_suite(ws, tmp_path, body, expected=2)
    base = ws.base_sha("fx-0001-ht1")
    got = Verifier(ws, LocalTestRunner(), host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=suite,
        runner_config=[], overlap=[],
    )  # fmt: skip
    assert not got["ok"] and got["failing"] == 1 and got["errors"] == 1
