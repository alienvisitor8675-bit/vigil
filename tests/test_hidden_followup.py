"""Hotfix 5: the follow-up to #18's review. The hidden checkout is the base plus
only the worker's in-scope changes, so nothing planted can run; an assertion
failure is one that carries the task's HMAC tag from vigild's helper; the number
of tests that ran must be the number stored; a vitest suite is injected only
where the project's own include collects it; and tests.json keeps no test names
or source lines."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

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
    Verifier,
    hidden_tag,
    parse_junit,
    runner_like,
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


# --- 1. black-box: the test process never holds worker code; the subject is a subprocess ----
#
# Reviewer repros for #19 at b450b4a, ported (each asserted the hole; now each must fail to flip)

BROAD = ["conftest.py", "tools/check.py", "**"]  # the most hostile scope there can be


def _attack(files: dict[str, str]) -> Callable[[Path], None]:
    """A worker that does not do the task (tools/check.py is never written, so the
    hidden test must fail on its head) but plants `files`, in or out of scope."""

    def edit(c: Path) -> None:
        (c / "tests" / "test_flag.py").write_text("def test_flag_env():\n    assert True\n")
        for rel, text in files.items():
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_text(text)

    return edit


RENAME = """

def pytest_collection_modifyitems(items):
    for it in items:
        it._nodeid = "{prefix}/" + it._nodeid
"""
ATTACKS = {
    "ini above the hidden dir": {
        ".vigil/fx-0001/hidden/pytest.ini": FLIPPER_INI,
        ".vigil/fx-0001/hidden/flipper.py": PLUGIN,
    },
    "pytest.toml above the hidden dir": {
        ".vigil/fx-0001/hidden/pytest.toml": '[pytest]\npythonpath = ["."]\naddopts = ["-p", "flipper"]\n',
        ".vigil/fx-0001/hidden/flipper.py": PLUGIN + RENAME.format(prefix=".vigil/fx-0001/hidden"),
    },
    "dot pytest.ini": {
        ".vigil/.pytest.ini": "[pytest]\npythonpath = .\naddopts = -p flipper\n",
        ".vigil/flipper.py": PLUGIN + RENAME.format(prefix=".vigil"),
    },
    "ini at the repo root": {"pytest.ini": FLIPPER_INI, "flipper.py": PLUGIN},
    "nested conftest.py": {"tests/sub/conftest.py": PLUGIN},
    "the flagged conftest.py with a hook": {
        "conftest.py": "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n" + PLUGIN
    },
    "a module that registers a hook through gc": {
        "src/feature.py": (
            "import gc\nimport pytest\n\n\nclass _F:\n"
            "    @pytest.hookimpl(hookwrapper=True)\n"
            "    def pytest_runtest_makereport(self, item, call):\n"
            "        out = yield\n        r = out.get_result()\n"
            "        if r.when == 'call':\n            r.outcome = 'passed'\n"
            "            r.longrepr = None\n\n\n"
            "for o in gc.get_objects():\n"
            "    if type(o).__name__ == 'Config' and hasattr(o, 'pluginmanager'):\n"
            "        o.pluginmanager.register(_F(), 'f')\n        break\n"
        ),
    },
    "vitest setup file": {
        "server/test/setup.ts": "process.env.FLAG_ON = '1'\n",
        "server/vitest.config.ts": "export default { test: { setupFiles: ['test/setup.ts'] } }\n",
    },
    "package.json script": {"package.json": '{"scripts": {"test": "FLAG_ON=1 pytest"}}\n'},
    "leftover under .vigil": {".vigil/fx-0001/note.txt": "x\n"},
}


@pytest.mark.parametrize("name", list(ATTACKS))
def test_a_planted_file_has_no_effect_on_hidden_results(
    ws: Workspace, tmp_path: Path, name: str
) -> None:
    base, head = _session(ws, tmp_path, _attack(ATTACKS[name]))
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path), scope=BROAD)["hidden"]
    # the task was not done, so the hidden test fails on the head, whatever was planted
    assert h["ok"] is False and h["passed_on_head"] is False, h


def test_a_hidden_test_that_imports_in_scope_code_is_rejected_and_cannot_flip(
    ws: Workspace, tmp_path: Path
) -> None:
    """The reviewer's in-scope module that reaches pytest's Config through gc."""
    body = (
        "from vigil_hidden import check\nfrom src.feature import flag\n\n\n"
        "def test_flag_env():\n    check(flag() is True)\n"
    )
    evil = ATTACKS["a module that registers a hook through gc"]["src/feature.py"]
    base, head = _session(
        ws,
        tmp_path,
        lambda c: (
            (c / "tests" / "test_flag.py").write_text("def test_flag_env():\n    assert True\n"),
            (c / "src" / "feature.py").write_text(evil + "\n\ndef flag():\n    return False\n"),
        ),
    )
    h = _verify_flagged(
        ws, base, head, _hidden_suite(ws, tmp_path, body), scope=["conftest.py", "src/**"]
    )["hidden"]
    assert h["ok"] is False and any("in-scope" in w for w in h["whitebox"]), h
    assert "base" not in h  # refused before any run


def test_a_subprocess_only_hidden_test_passes_a_correct_implementation_and_fails_a_wrong_one(
    ws: Workspace, tmp_path: Path
) -> None:
    def edit(ok: bool) -> Callable[[Path], None]:
        def run(c: Path) -> None:
            _runner_edit(c)
            (c / "tools" / "check.py").write_text('print("ok")\n' if ok else 'print("nope")\n')

        return run

    base, head = _session(ws, tmp_path, edit(True))
    assert _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))["hidden"]["ok"] is True
    ws2 = ws
    b2, h2 = _session(ws2, tmp_path / "w", edit(False))
    bad = _verify_flagged(ws2, b2, h2, _hidden_suite(ws2, tmp_path / "w"))["hidden"]
    assert bad["ok"] is False and bad["passed_on_head"] is False
    assert bad["head"]["outcome"] == "fail"


def test_the_hidden_checkout_has_nothing_outside_scope(ws: Workspace, tmp_path: Path) -> None:
    """The subject is the base plus in-scope changes only; the test process has no
    head file at all."""
    body = (
        "import os\nfrom vigil_hidden import check, run_subject\n\n\ndef test_h():\n"
        "    check(not os.path.exists('.vigil/fx-0001/note.txt'))\n"
        "    check(not os.path.exists('tests/sub/conftest.py'))\n"
        "    r = run_subject(['python3', 'tools/check.py'])\n"
        "    check(r['stdout'].strip() == 'ok', repr(r))\n"
        '    probe = \'import os; print(os.path.exists("tests/sub/conftest.py"), '
        'os.path.exists(".vigil/fx-0001/note.txt"))\'\n'
        "    p = run_subject(['python3', '-c', probe])\n"
        "    check(p['stdout'].strip() == 'False False', repr(p))\n"
    )

    def edit(c: Path) -> None:
        _runner_edit(c)
        for rel in (".vigil/fx-0001/note.txt", "tests/sub/conftest.py"):
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_text("planted\n")

    base, head = _session(ws, tmp_path, edit)
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path, body))["hidden"]
    assert h["passed_on_head"] is True and h["ok"] is True, h


def test_the_subject_checkout_is_read_only_to_nothing_but_the_runner_mounts(
    ws: Workspace, tmp_path: Path
) -> None:
    """The local runner is told where the subject is; the test process's own
    checkout (its cwd) is a different directory."""
    runner = LocalTestRunner()
    base, head = _session(ws, tmp_path, _runner_edit)
    _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path), runner=runner)
    hidden_calls = [c for c in runner.calls if "test_hidden.py" in c["command"]]
    assert len(hidden_calls) == 2
    assert all(c["subject"] and c["subject"] != c["source"] for c in hidden_calls)
    assert hidden_calls[0]["subject"] != hidden_calls[1]["subject"]


def test_a_symlinked_hidden_parent_cannot_make_vigild_write_outside(
    ws: Workspace, tmp_path: Path
) -> None:
    import os

    outside = tmp_path / "host-outside"
    outside.mkdir()

    def edit(c: Path) -> None:
        (c / "tests" / "test_flag.py").write_text("def test_flag_env():\n    assert True\n")
        (c / ".vigil" / "fx-0001").mkdir(parents=True, exist_ok=True)
        os.symlink(str(outside), c / ".vigil" / "fx-0001" / "hidden")

    base, head = _session(ws, tmp_path, edit)
    h = _verify_flagged(
        ws, base, head, _hidden_suite(ws, tmp_path), scope=["conftest.py", ".vigil/**"]
    )["hidden"]
    assert h["ok"] is False
    assert sorted(outside.rglob("*")) == []


def test_an_in_scope_runner_config_file_that_is_not_the_flagged_one_is_tampering(
    ws: Workspace, tmp_path: Path
) -> None:
    def edit(c: Path) -> None:
        _runner_edit(c)
        (c / "tests" / "sub").mkdir()
        (c / "tests" / "sub" / "conftest.py").write_text("# in scope, but not the runner file\n")

    base, head = _session(ws, tmp_path, edit)
    suite = _hidden_suite(ws, tmp_path)
    h = _verify_flagged(ws, base, head, suite, scope=["conftest.py", "tools/check.py", "tests/**"])
    assert h["hidden"]["ok"] is False and h["hidden"]["tampered"] == ["tests/sub/conftest.py"]


def test_the_flagged_file_runs_from_the_head_as_a_subprocess(ws: Workspace, tmp_path: Path) -> None:
    base, head = _session(ws, tmp_path, _runner_edit)
    h = _verify_flagged(ws, base, head, _hidden_suite(ws, tmp_path))["hidden"]
    assert h["ok"] is True and h["head"]["outcome"] == "pass"


def test_runner_like_names_are_matched_at_any_depth() -> None:
    for name in (
        "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "a/b/conftest.py", "tox.ini",
        "setup.cfg", "pyproject.toml", ".npmrc", "x/vitest.config.ts", "vitest.unit.config.mts",
        "vitest.workspace.ts", "vite.config.js",
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


# --- 4. placement is asked of the runner, never read from its config ---------------------------

PKG = '{"scripts": {"test": "vitest run"}}'


def _read(files: dict[str, str]) -> Any:
    return lambda p: files.get(p)


def _collects(*dirs: str, suffix: str = ".test.ts") -> Any:
    """A stand-in for `vitest list --json`: it collects probe files in these dirs."""

    def probe(files: list[str]) -> set[str] | None:
        return {f for f in files if any(f.startswith(d + "/") for d in dirs) and f.endswith(suffix)}

    return probe


def test_a_vitest_task_gets_the_first_directory_the_runner_collects() -> None:
    read = _read({"server/package.json": PKG})
    place = place_for("j-1", "j-1-w1", "pnpm -C server test", read, _collects("server/test"))
    assert place.folder == "server/test/vigil-hidden/j-1/j-1-w1"
    assert place.project == "server" and place.vitest and place.suffix == ".test.ts"
    spec = place_for(
        "j-1", "j-1-w1", "pnpm -C server test", read, _collects("server/src", suffix=".spec.ts")
    )
    assert spec.folder == "server/src/vigil-hidden/j-1/j-1-w1" and spec.suffix == ".spec.ts"


def test_a_python_task_keeps_the_vigil_directory_and_asks_nothing() -> None:
    def never(files: list[str]) -> set[str] | None:
        raise AssertionError("pytest takes explicit paths; nothing to probe")

    place = place_for("j-1", "j-1-w1", "pytest -q tests", _read({}), never)
    assert place.folder == ".vigil/j-1/hidden/j-1-w1" and not place.vitest


def test_a_runner_that_cannot_be_asked_or_collects_nothing_gets_no_hidden_tests() -> None:
    read = _read({"server/package.json": PKG})
    with pytest.raises(HiddenError, match="could not be asked"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read, lambda files: None)
    with pytest.raises(HiddenError, match="collects none"):
        place_for("j-1", "j-1-w1", "pnpm -C server test", read, lambda files: set())


def test_the_vitest_config_text_is_never_parsed() -> None:
    """A `coverage.include` or a `--config` flag can't mislead: only the runner's answer counts."""
    cfg = "export default { test: { coverage: { include: ['src/**'] } } }"
    read = _read({"server/package.json": PKG, "server/vitest.config.ts": cfg})
    place = place_for("j-1", "j-1-w1", "pnpm -C server test", read, _collects("server/tests"))
    assert place.folder == "server/tests/vigil-hidden/j-1/j-1-w1"


def test_the_list_command_is_the_projects_own_script_in_list_mode() -> None:
    from vigild.verifier import vitest_list_command

    read = _read(
        {"server/package.json": '{"scripts": {"test": "vitest run --config vt.config.ts"}}'}
    )
    cmd = vitest_list_command("pnpm -C server test", read)
    assert cmd and "vitest list --filesOnly --json" in cmd and "--config vt.config.ts" in cmd
    assert "--reporter" not in cmd and "junit" not in cmd
    assert vitest_list_command("pytest -q tests", read) is None


def test_the_runners_list_output_is_parsed_from_noise() -> None:
    from vigild.verifier import _listed_files

    out = 'warn [x]\n[{"file": "/work/server/test/a.test.ts", "name": "a"}]\ndone\n'
    assert _listed_files(out) == ["/work/server/test/a.test.ts"]
    assert _listed_files("no json here") is None


def test_collect_probe_asks_the_runner_on_the_base(
    ws: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigild import verifier
    from vigild.verifier import RunResult

    seen: list[tuple[str, bool]] = []

    class Lister:
        def run(self, image: str, source: Path, command: str, **kw: Any) -> RunResult:
            probe = next(source.rglob("vigil-probe.test.ts"))
            seen.append((command, probe.exists()))
            return RunResult(
                0, "warn\n" + json.dumps([{"file": f"/work/{probe.relative_to(source)}"}])
            )

    monkeypatch.setattr(verifier, "vitest_list_command", lambda cmd, read: "vitest-list")
    v = Verifier(ws, Lister(), host_key=KEY)  # type: ignore[arg-type]
    base, _ = _session(ws, tmp_path, _runner_edit)
    files = ["a/vigil-probe.test.ts", "b/vigil-probe.test.ts"]
    got = v.collect_probe(REPO, "img", base, "pnpm test", files)
    assert seen == [("vitest-list", True)]
    assert got is not None and len(got) == 1  # only the one the runner listed
    monkeypatch.setattr(verifier, "vitest_list_command", lambda cmd, read: None)
    assert v.collect_probe(REPO, "img", base, "pytest -q", files) is None


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
    assert "test_tool_says_ok" in (ws.data_dir / "hidden-runs" / "fx-0001-w1-hbase.txt").read_text()


def test_the_helper_is_laid_per_language_and_reads_the_tag_from_the_environment() -> None:
    from vigild.verifier import HELPER_NAME, TAG_ENV, helper_file

    py_path, py = helper_file("d/p", vitest=False)
    ts_path, ts = helper_file("d/p", vitest=True)
    assert py_path == f"d/p/{HELPER_NAME}.py" and ts_path == f"d/p/{HELPER_NAME}.mjs"
    assert TAG_ENV in py and f"process.env.{TAG_ENV}" in ts and "AssertionError" in py + ts
    assert TAG not in py + ts  # the tag is never in a file, only in the base run's environment
