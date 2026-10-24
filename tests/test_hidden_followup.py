"""Hotfix 5: the follow-up to #18's review. The hidden checkout is the base plus
only the worker's in-scope changes, so nothing planted can run; an assertion
failure is one that carries the task's HMAC tag from vigild's helper; the number
of tests that ran must be the number stored; a vitest suite is injected only
where the project's own include collects it; and tests.json keeps no test names
or source lines."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.harness import LocalTestRunner
from tests.test_verifier import (  # noqa: F401
    HIDDEN_DIR,
    KEY,
    REPO,
    TID,
    _hidden_suite,
    _runner_edit,
    _session,
    _verify_flagged,
    ws,
)
from vigild.hidden import HiddenError, place_for
from vigild.verifier import (
    HiddenSuite,
    Verifier,
    collected,
    hidden_tag,
    parse_junit,
    runner_like,
    uncollected,
)
from vigild.workspace import Workspace

PLUGIN = """import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    r = out.get_result()
    if r.when == "call":
        r.outcome = "passed"
        r.longrepr = None
"""
FLIPPER_INI = "[pytest]\naddopts = -p flipper\n"
TAG = hidden_tag(KEY, TID)


# --- 1. the hidden checkout is the base plus in-scope worker changes only -------------------


def _attack(files: dict[str, str]) -> Callable[[Path], None]:
    """A worker that does not fix the runner file (conftest.py unchanged, so the
    hidden test must fail on its head) but plants `files` outside its scope."""

    def edit(c: Path) -> None:
        (c / "tests" / "test_flag.py").write_text("def test_flag_env():\n    assert True\n")
        for rel, text in files.items():
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_text(text)

    return edit


ATTACKS = {
    "ini above the hidden dir": {
        ".vigil/fx-0001/hidden/pytest.ini": FLIPPER_INI,
        ".vigil/fx-0001/hidden/flipper.py": PLUGIN,
    },
    "ini at the repo root": {"pytest.ini": FLIPPER_INI, "flipper.py": PLUGIN},
    "nested conftest.py": {"tests/sub/conftest.py": "import os\nos.environ['FLAG_ON'] = '1'\n"},
    "vitest setup file": {
        "server/test/setup.ts": "process.env.FLAG_ON = '1'\n",
        "server/vitest.config.ts": "export default { test: { setupFiles: ['test/setup.ts'] } }\n",
    },
    "package.json script": {"package.json": '{"scripts": {"test": "FLAG_ON=1 pytest"}}\n'},
    "leftover under .vigil": {".vigil/fx-0001/note.txt": "x\n"},
}


@pytest.mark.parametrize("name", list(ATTACKS))
def test_a_planted_file_outside_scope_has_no_effect_on_hidden_results(
    ws: Workspace, tmp_path: Path, name: str
) -> None:
    base, head = _session(ws, tmp_path, _attack(ATTACKS[name]))
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))["hidden"]
    # the real fix (conftest.py in scope) was not delivered: the test fails on the head
    # exactly as if nothing had been planted
    assert h["ok"] is False and h["passed_on_head"] is False and "tampered" not in h
    assert h["head"]["outcome"] == "fail"


def test_the_hidden_checkout_has_nothing_outside_scope(ws: Workspace, tmp_path: Path) -> None:
    """Out-of-scope head files never enter the checkout: the hidden test itself
    looks for them."""
    body = (
        "import os\nfrom vigil_hidden import check\n\n\ndef test_h():\n"
        "    check(os.environ.get('FLAG_ON') == '1')\n"
        "    check(not os.path.exists('.vigil/fx-0001/note.txt'))\n"
        "    check(not os.path.exists('tests/sub/conftest.py'))\n"
    )

    def edit(c: Path) -> None:
        _runner_edit(c)
        for rel in (".vigil/fx-0001/note.txt", "tests/sub/conftest.py"):
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_text("planted\n")

    base, head = _session(ws, tmp_path, edit)
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path, body))["hidden"]
    assert h["passed_on_head"] is True and h["ok"] is True


def test_an_in_scope_runner_config_file_that_is_not_the_flagged_one_is_tampering(
    ws: Workspace, tmp_path: Path
) -> None:
    def edit(c: Path) -> None:
        _runner_edit(c)
        (c / "tests" / "sub").mkdir()
        (c / "tests" / "sub" / "conftest.py").write_text("# in scope, but not the runner file\n")

    base, head = _session(ws, tmp_path, edit)
    suite = _hidden_suite(ws, tmp_path)
    h = _verify_flagged(ws, base, head, suite, scope=["conftest.py", "tests/**"])["hidden"]
    assert h["ok"] is False and h["tampered"] == ["tests/sub/conftest.py"]


def test_the_flagged_runner_file_itself_comes_from_the_head(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))["hidden"]
    assert h["ok"] is True and h["head"]["outcome"] == "pass"


def test_runner_like_names_are_matched_at_any_depth() -> None:
    for name in (
        "pytest.ini", "a/b/conftest.py", "tox.ini", "setup.cfg", "pyproject.toml", ".npmrc",
        "x/vitest.config.ts", "vitest.unit.config.mts", "vite.config.js",
    ):  # fmt: skip
        assert runner_like(name), name
    assert not runner_like("tests/test_a.py") and not runner_like("a/vitest.ts")


# --- 2. an assertion failure carries the task's HMAC tag ------------------------------------


def _suite_xml(failure: str) -> bytes:
    return (
        f'<testsuites><testsuite name="s" tests="1"><testcase classname="t" name="a">'
        f"{failure}</testcase></testsuite></testsuites>"
    ).encode()


PYTEST = {
    "assert": f'<failure message="AssertionError: [vigil-tag:{TAG}] flag is off">x</failure>',
    "bare assert": '<failure message="assert 1 == 2">def test_a():\n&gt;   assert 1 == 2</failure>',
    "thrown": "<failure message=\"AttributeError: module 'm' has no attribute 'y'\">x</failure>",
    "import": "<failure message=\"ImportError: cannot import name 'z' from 'm'\">x</failure>",
    "timeout": '<failure message="Failed: Timeout &gt;5.0s">x</failure>',
    "error": '<error message="collection failure">x</error>',
}
VITEST = {
    "assert": f'<failure message="[vigil-tag:{TAG}] flag is off" type="AssertionError">x</failure>',
    "bare expect": '<failure message="expected 1 to be 2" type="AssertionError">x</failure>',
    "thrown": '<failure message="boom" type="Error">x</failure>',
    "import": '<failure message="Failed to resolve import &quot;./nope&quot; from '
    '&quot;test/a.test.ts&quot;. Does the file exist?" type="Error">x</failure>',
    "timeout": '<failure message="Test timed out in 5000ms.\nIf this is a long-running test, '
    'use setTimeout" type="Error">x</failure>',
}


@pytest.mark.parametrize("runner", [PYTEST, VITEST], ids=["pytest", "vitest"])
def test_only_a_tagged_failure_is_fail_on_base(runner: dict[str, str]) -> None:
    for kind, xml in runner.items():
        (case,) = parse_junit(_suite_xml(xml), TAG)
        run = {"_cases": [case], "_report": True, "outcome": "fail"}
        got = Verifier.base_failure(run, 1)
        assert got["ok"] is (kind == "assert"), kind
        if kind != "assert":
            assert got["errors"] == 1 and got["failing"] == 0 and "assertion" in got["why"]


def test_a_failure_carrying_another_tasks_tag_or_a_guess_does_not_count() -> None:
    other = hidden_tag(KEY, "fx-0001-w2")
    wrong_key = hidden_tag(b"x" * 32, TID)
    for tag in (other, wrong_key, "0" * 32):
        xml = f'<failure message="AssertionError: [vigil-tag:{tag}] forged">x</failure>'
        (case,) = parse_junit(_suite_xml(xml), TAG)
        assert case.assertion is False
    assert len({TAG, other, wrong_key}) == 3


def test_a_hidden_test_that_forges_the_tag_without_the_key_does_not_count(
    ws: Workspace, tmp_path: Path
) -> None:
    body = f"def test_h():\n    raise AssertionError('[vigil-tag:{'0' * 32}] an assertion')\n"
    suite = _hidden_suite(ws, tmp_path, body)
    base = ws.base_sha("fx-0001-ht1")
    got = Verifier(ws, LocalTestRunner(), host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id=TID, base_sha=base, hidden=suite,
        runner_config=["conftest.py"], overlap=["conftest.py"],
    )  # fmt: skip
    assert got["ok"] is False and "assertion" in got["why"]


def test_a_bare_assert_in_a_hidden_test_does_not_count_on_the_base(
    ws: Workspace, tmp_path: Path
) -> None:
    suite = _hidden_suite(ws, tmp_path, "def test_h():\n    assert False\n")
    base = ws.base_sha("fx-0001-ht1")
    got = Verifier(ws, LocalTestRunner(), host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id=TID, base_sha=base, hidden=suite,
        runner_config=["conftest.py"], overlap=["conftest.py"],
    )  # fmt: skip
    assert got["ok"] is False


def test_without_the_host_key_no_hidden_test_counts(ws: Workspace, tmp_path: Path) -> None:
    suite = _hidden_suite(ws, tmp_path)
    base = ws.base_sha("fx-0001-ht1")
    got = Verifier(ws, LocalTestRunner()).check_hidden_on_base(
        repo=REPO, image="img", task_id=TID, base_sha=base, hidden=suite,
    )  # fmt: skip
    assert got["ok"] is False and "VIGIL_HOST_KEY" in got["why"]
    b, head = _session(ws, tmp_path, _runner_edit)
    rec = Verifier(ws, LocalTestRunner()).verify(
        repo=REPO, image="img", task_id=TID, base_sha=b, head=head, test_cmd="pytest -q tests",
        must_fail_first=True, runner_config=["conftest.py"], runner_overlap=["conftest.py"],
        hidden=suite,
    )  # fmt: skip
    assert rec["hidden"]["ok"] is False


# --- 5. the number of tests that ran is the number stored -----------------------------------


def test_more_tests_ran_than_are_stored_is_a_fail_on_both_sides(
    ws: Workspace,
    tmp_path: Path,
) -> None:
    two = (
        "from vigil_hidden import check\n\n\ndef test_a():\n    check(False)\n\n\n"
        "def test_b():\n    check(False)\n"
    )
    base, head = _session(ws, tmp_path, _runner_edit)
    suite = _hidden_suite(ws, tmp_path, two, expected=1)
    got = Verifier(ws, LocalTestRunner(), host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=suite,
        runner_config=["conftest.py"], overlap=["conftest.py"],
    )  # fmt: skip
    assert got["ok"] is False and "2 hidden test(s) ran" in got["why"]
    rec = _verify_flagged(ws, base, head, suite)
    assert rec["hidden"]["ok"] is False and rec["hidden"]["failed_on_base"] is False
    assert rec["hidden"]["count"]["expected"] == 1 and rec["hidden"]["count"]["base"] == 2


def test_fewer_tests_ran_than_are_stored_is_a_fail(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    suite = _hidden_suite(ws, tmp_path, expected=2)  # one test file with one test
    rec = _verify_flagged(ws, base, head, suite)
    h = rec["hidden"]
    assert h["ok"] is False and h["count"] == {"expected": 2, "base": 1, "head": 1, "ok": False}


def test_zero_tests_that_ran_is_a_fail() -> None:
    run = {"_cases": [], "_report": True, "outcome": "pass"}
    got = Verifier.base_failure(run, 3)
    assert got["ok"] is False and got["tests"] == 0


def test_the_stored_number_ran_on_both_sides_is_ok(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))["hidden"]
    assert h["ok"] and h["count"] == {"expected": 1, "base": 1, "head": 1, "ok": True}


# --- 4. the injection path is one the project's vitest include collects ----------------------

CONFIG = """import { defineConfig } from 'vitest/config'
export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    exclude: ['test/fixtures/**'],
  },
})
"""
PKG = '{"scripts": {"test": "vitest run"}}'


def _read(files: dict[str, str]) -> Any:
    return lambda p: files.get(p)


def test_a_vitest_task_gets_a_directory_its_include_collects() -> None:
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": CONFIG})
    place = place_for("j-1", "j-1-w1", "pnpm -C server test", read)
    assert place.folder == "server/test/vigil-hidden/j-1/j-1-w1"
    assert place.project == "server" and place.include == ("test/**/*.test.ts",)
    # the files are injected there and collected; a name the include skips is refused
    suite = HiddenSuite("a" * 40, place.folder, (f"{place.folder}/a.test.ts",), "x", 1,
                        place.project, place.include)  # fmt: skip
    assert uncollected(suite) == []
    bad = dataclasses.replace(suite, files=(f"{place.folder}/a.spec.ts",))
    assert uncollected(bad) == [f"{place.folder}/a.spec.ts"]


def test_a_python_task_keeps_the_vigil_directory() -> None:
    place = place_for("j-1", "j-1-w1", "pytest -q tests", _read({}))
    assert place.folder == ".vigil/j-1/hidden/j-1-w1" and place.include is None


def test_a_vitest_include_that_cannot_be_read_gets_no_hidden_tests() -> None:
    read = _read({"server/package.json": PKG})
    with pytest.raises(HiddenError, match="no vitest config"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read)
    ws_cfg = "export default { test: { projects: ['a', 'b'] } }"
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": ws_cfg})
    with pytest.raises(HiddenError, match="projects"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read)
    odd = CONFIG.replace("test/**/*.test.ts", "test/**/*.nope.ts")
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": odd})
    with pytest.raises(HiddenError, match="would not collect"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read)
    spec_only = CONFIG.replace("test/**/*.test.ts", "test/**/*.spec.ts")
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": spec_only})
    place = place_for("j-1", "j-1-w1", "pnpm -C server test", read)
    assert place.folder == "server/test/vigil-hidden/j-1/j-1-w1"
    excluded = CONFIG.replace("test/fixtures/**", "test/**")
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": excluded})
    with pytest.raises(HiddenError, match="would not collect"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read)


def test_the_default_vitest_include_collects_test_and_spec_files() -> None:
    assert collected(None, "a/b.test.ts") and collected((), "x.spec.mjs")
    assert not collected((), "a/helper.ts")
    assert collected(("src/**/*.{test,spec}.ts",), "src/deep/x.spec.ts")
    assert not collected(("src/**/*.test.ts",), "test/x.test.ts")


def test_a_suite_the_include_would_not_collect_is_refused_before_any_run(
    ws: Workspace,
    tmp_path: Path,
) -> None:
    suite = dataclasses.replace(_hidden_suite(ws, tmp_path), project="", include=("test/**/*.ts",))
    base = ws.base_sha("fx-0001-ht1")
    runner = LocalTestRunner()
    got = Verifier(ws, runner, host_key=KEY).check_hidden_on_base(
        repo=REPO, image="img", task_id="fx-0001-w1", base_sha=base, hidden=suite,
    )  # fmt: skip
    assert got["ok"] is False and "include" in got["why"] and runner.calls == []
    b, head = _session(ws, tmp_path, _runner_edit)
    h = _verify_flagged(ws, b, head, suite)["hidden"]
    assert h["ok"] is False and h["uncollected"] == list(suite.files)


# --- 7. tests.json keeps counts, an ids hash and a category; the output stays on the host ----


def test_the_hidden_record_has_no_output_names_or_source(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    rec = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))
    h = rec["hidden"]
    text = json.dumps({k: v for k, v in h.items() if k != "test_cmd"})
    assert "tail" not in text and "test_hidden" not in text and "FLAG_ON" not in text
    assert h["base"]["failure_category"] == "assertion" and h["head"]["failure_category"] == "none"
    assert h["base"]["assertion_failures"] == 1 and len(h["base"]["ids_hash"]) == 16
    out = sorted((ws.data_dir / "hidden-runs").glob("*.txt"))
    assert {p.name for p in out} == {
        "fx-0001-w1-hbase.txt",
        "fx-0001-w1-hhead.txt",
    }
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in out)
    assert "FLAG_ON" in (ws.data_dir / "hidden-runs" / "fx-0001-w1-hbase.txt").read_text()


def test_the_helper_is_laid_per_language_and_reads_the_tag_from_the_environment() -> None:
    from vigild.verifier import HELPER_NAME, TAG_ENV, helper_file

    py_path, py = helper_file("d/p", vitest=False)
    ts_path, ts = helper_file("d/p", vitest=True)
    assert py_path == f"d/p/{HELPER_NAME}.py" and ts_path == f"d/p/{HELPER_NAME}.ts"
    assert TAG_ENV in py and f"process.env.{TAG_ENV}" in ts and "AssertionError" in py + ts
    assert TAG not in py + ts  # the tag is never in a file, only in the base run's environment
