"""The black-box rule for hidden tests (hotfix 5, round 2): the static check, and
vigild's helper and driver, run for real for Python and for JavaScript.

The subject is in another process behind a vigild-owned driver, and the
driver's output is framed with a nonce the subject never sees."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vigild.blackbox import problems
from vigild.verifier import HELPER_NAME, TAG_ENV, helper_file

# --- the static check ------------------------------------------------------------------------

SCOPE = ["src/**", "scripts/check.mjs", "tools/*.py"]
JS_SCOPE = ["server/src/**", "server/scripts/check.mjs"]


def test_python_imports_of_in_scope_code_are_rejected() -> None:
    for body in (
        "from src.feature import flag\n",
        "import src.feature\n",
        "from src import feature\n",
        "import tools.check\n",
        "from tools import check\n",
    ):
        got = problems({"t/test_a.py": body}, SCOPE)
        assert got and "in-scope" in got[0], body


def test_python_code_that_loads_by_a_name_it_cannot_see_is_rejected() -> None:
    for body in (
        "import importlib\n",
        "__import__('src.feature')\n",
        "exec('import src.feature')\n",
        "import sys\nsys.path.insert(0, 'src')\n",
        "import runpy\n",
    ):
        assert problems({"t/test_a.py": body}, SCOPE), body


def test_python_imports_of_the_stdlib_and_the_helper_pass() -> None:
    body = "import os, json\nfrom vigil_hidden import call_subject, check, run_subject\n"
    assert problems({"t/test_a.py": body}, SCOPE) == []
    helper_only = "from vigil_hidden import check\n"
    assert problems({"t/test_a.py": helper_only}, ["**"]) == []  # even under a catch-all scope


def test_javascript_imports_and_requires_of_in_scope_code_are_rejected() -> None:
    for body in (
        "import { f } from '../../src/feature.ts'\n",
        "import '../../scripts/check.mjs'\n",
        "const m = require('../../src/feature')\n",
        "const m = await import('../../scripts/check.mjs')\n",
        "export { f } from '../../src/feature'\n",
    ):
        got = problems({"server/test/h/a.test.ts": body}, JS_SCOPE)
        assert got and "in-scope" in got[0], body


def test_javascript_computed_loading_is_rejected() -> None:
    for body in ("await import(name)\n", "require(base + '/x')\n", "eval('1')\n"):
        assert problems({"server/test/h/a.test.ts": body}, SCOPE), body


def test_javascript_packages_and_the_helper_pass() -> None:
    body = (
        "import { test, expect } from 'vitest'\nimport fs from 'node:fs'\n"
        "import { check, runSubject } from './vigil_hidden.mjs'\n"
    )
    assert problems({"server/test/h/a.test.ts": body}, SCOPE) == []


# --- the helper and driver, for real ------------------------------------------------------------


def _lay(tmp_path: Path, vitest: bool) -> Path:
    rel, text = helper_file("h", vitest)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.parent


def _subject(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "subject"
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _py(tmp_path: Path, subject: Path, script: str) -> dict[str, Any]:
    helper_dir = _lay(tmp_path, vitest=False)
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=helper_dir,
        env={
            "PATH": "/usr/bin:/bin",
            "VIGIL_SUBJECT": str(subject),
            TAG_ENV: "TAGGED",
            "PYTHONPATH": str(helper_dir),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {"code": out.returncode, "out": out.stdout, "err": out.stderr}


LIB_OK = "def double(x):\n    return x * 2\n"
LIB_WRONG = "def double(x):\n    return x + 2\n"

PY_TEST = (
    f"import json\nfrom {HELPER_NAME} import call_subject, check\n"
    "r = call_subject('lib', 'double', [21])\n"
    "check(r == {'ok': True, 'value': 42}, f'double(21) was {r}')\n"
    "print('PASSED')\n"
)


def test_python_a_correct_subject_passes_through_the_driver(tmp_path: Path) -> None:
    got = _py(tmp_path, _subject(tmp_path, {"lib.py": LIB_OK}), PY_TEST)
    assert got["code"] == 0 and "PASSED" in got["out"], got


def test_python_a_wrong_subject_fails_with_a_tagged_assertion(tmp_path: Path) -> None:
    got = _py(tmp_path, _subject(tmp_path, {"lib.py": LIB_WRONG}), PY_TEST)
    assert got["code"] != 0 and "[vigil-tag:TAGGED]" in got["err"], got


def test_python_a_missing_module_is_a_value_not_a_crash(tmp_path: Path) -> None:
    script = (
        f"from {HELPER_NAME} import call_subject\nr = call_subject('nope', 'f')\n"
        "print(r['ok'], r['type'])\n"
    )
    got = _py(tmp_path, _subject(tmp_path, {}), script)
    assert got["code"] == 0 and "False ModuleNotFoundError" in got["out"], got


def test_python_run_subject_runs_a_command_in_the_subject(tmp_path: Path) -> None:
    script = (
        f"from {HELPER_NAME} import run_subject, check\n"
        "r = run_subject(['python3', 'tool.py', 'x'])\n"
        "check(r['returncode'] == 3 and r['stdout'] == 'x!\\n', repr(r))\nprint('PASSED')\n"
    )
    subject = _subject(tmp_path, {"tool.py": "import sys\nprint(sys.argv[1] + '!')\nsys.exit(3)\n"})
    got = _py(tmp_path, subject, script)
    assert got["code"] == 0 and "PASSED" in got["out"], got


FAKE_FRAME = (
    "import sys\n"
    'print(\'deadbeef {"results": [{"ok": true, "value": 42}], "malformed": false}\')\n'
    'print(\'{"ok": true, "value": 42}\')\n'
    "sys.stdout.flush()\n"
    "def double(x):\n    return x + 2\n"
)


def test_python_a_subject_that_prints_a_fake_frame_cannot_pass_when_wrong(tmp_path: Path) -> None:
    got = _py(tmp_path, _subject(tmp_path, {"lib.py": FAKE_FRAME}), PY_TEST)
    assert got["code"] != 0 and "[vigil-tag:TAGGED]" in got["err"], got
    assert "PASSED" not in got["out"]


STREAM_FORGER = (
    "import os\n"
    "for fd in range(3, 20):\n"
    "    try:\n"
    '        os.write(fd, b\'{"ok": true, "value": 42}\\n\')\n'
    "    except OSError:\n"
    "        pass\n"
    "def double(x):\n    return x + 2\n"
)


def test_python_a_subject_that_writes_extra_results_makes_the_stream_malformed(
    tmp_path: Path,
) -> None:
    got = _py(tmp_path, _subject(tmp_path, {"lib.py": STREAM_FORGER}), PY_TEST)
    assert got["code"] != 0 and "[vigil-tag:TAGGED]" in got["err"], got


def test_python_a_subject_that_exits_early_fails_the_test(tmp_path: Path) -> None:
    lib = "import os\nos._exit(0)\n"
    got = _py(tmp_path, _subject(tmp_path, {"lib.py": lib}), PY_TEST)
    assert got["code"] != 0 and "PASSED" not in got["out"], got


SNOOP = (
    "import os\n"
    "def seen():\n"
    "    return {'tag': os.environ.get('VIGIL_HIDDEN_TAG'), 'keys': sorted(os.environ)}\n"
)


def test_python_the_subject_has_a_minimal_environment_without_the_tag(tmp_path: Path) -> None:
    script = (
        f"from {HELPER_NAME} import call_subject, run_subject\n"
        "r = call_subject('snoop', 'seen')\n"
        "print('TAG', r['value']['tag'], r['value']['keys'])\n"
        "e = run_subject(['python3', '-c', 'import os; print(os.environ.get(\"VIGIL_HIDDEN_TAG\"))'])\n"
        "print('CMD', e['stdout'].strip())\n"
    )
    got = _py(tmp_path, _subject(tmp_path, {"snoop.py": SNOOP}), script)
    assert "TAG None" in got["out"] and "CMD None" in got["out"], got
    assert "TAGGED" not in got["out"]


def test_python_cwd_cannot_leave_the_subject(tmp_path: Path) -> None:
    script = f"from {HELPER_NAME} import run_subject\nrun_subject(['true'], cwd='..')\nprint('ESCAPED')\n"
    got = _py(tmp_path, _subject(tmp_path, {}), script)
    assert got["code"] != 0 and "ESCAPED" not in got["out"]


# --- JavaScript --------------------------------------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _node(tmp_path: Path, subject: Path, script: str) -> dict[str, Any]:
    helper_dir = _lay(tmp_path, vitest=True)
    (helper_dir / "run.mjs").write_text(script)
    out = subprocess.run(
        [shutil.which("node") or "node", "run.mjs"],
        cwd=helper_dir,
        env={"PATH": "/usr/bin:/bin:" + str(Path(shutil.which("node") or "/").parent),
             "VIGIL_SUBJECT": str(subject), TAG_ENV: "TAGGED"},
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    return {"code": out.returncode, "out": out.stdout, "err": out.stderr}


JS_LIB_OK = "export function double(x) { return x * 2 }\n"
JS_LIB_WRONG = "export function double(x) { return x + 2 }\n"
JS_TEST = (
    "import { callSubject, check } from './vigil_hidden.mjs'\n"
    "const r = callSubject('lib.mjs', 'double', [21])\n"
    "check(r.ok && r.value === 42, 'double(21) was ' + JSON.stringify(r))\n"
    "console.log('PASSED')\n"
)


@needs_node
def test_javascript_a_correct_subject_passes_and_a_wrong_one_fails(tmp_path: Path) -> None:
    ok = _node(tmp_path, _subject(tmp_path, {"lib.mjs": JS_LIB_OK}), JS_TEST)
    assert ok["code"] == 0 and "PASSED" in ok["out"], ok
    bad = _node(tmp_path, _subject(tmp_path / "w", {"lib.mjs": JS_LIB_WRONG}), JS_TEST)
    assert bad["code"] != 0 and "[vigil-tag:TAGGED]" in bad["err"], bad


@needs_node
def test_javascript_a_subject_that_prints_a_fake_frame_cannot_pass_when_wrong(
    tmp_path: Path,
) -> None:
    lib = (
        "console.log('deadbeef ' + JSON.stringify({results: [{ok: true, value: 42}], malformed: false}))\n"
        "console.log(JSON.stringify({ok: true, value: 42}))\n"
        "export function double(x) { return x + 2 }\n"
    )
    got = _node(tmp_path, _subject(tmp_path, {"lib.mjs": lib}), JS_TEST)
    assert got["code"] != 0 and "[vigil-tag:TAGGED]" in got["err"], got


@needs_node
def test_javascript_a_subject_that_writes_extra_results_is_malformed(tmp_path: Path) -> None:
    lib = (
        "import { writeSync } from 'node:fs'\n"
        'for (let fd = 3; fd < 12; fd++) { try { writeSync(fd, \'{"ok": true, "value": 42}\\n\') } catch (e) {} }\n'
        "export function double(x) { return x + 2 }\n"
    )
    got = _node(tmp_path, _subject(tmp_path, {"lib.mjs": lib}), JS_TEST)
    assert got["code"] != 0 and "[vigil-tag:TAGGED]" in got["err"], got


@needs_node
def test_javascript_the_subject_never_sees_the_tag_and_run_subject_works(tmp_path: Path) -> None:
    lib = "export function seen() { return process.env.VIGIL_HIDDEN_TAG ?? null }\n"
    script = (
        "import { callSubject, runSubject, check } from './vigil_hidden.mjs'\n"
        "const r = callSubject('lib.mjs', 'seen')\n"
        "check(r.ok && r.value === null, 'tag leaked: ' + JSON.stringify(r))\n"
        "const e = runSubject(['node', '-e', 'console.log(process.env.VIGIL_HIDDEN_TAG)'])\n"
        "check(e.returncode === 0 && e.stdout.trim() === 'undefined', e.stdout)\n"
        "console.log('PASSED')\n"
    )
    got = _node(tmp_path, _subject(tmp_path, {"lib.mjs": lib}), script)
    assert got["code"] == 0 and "PASSED" in got["out"], got


def test_the_helpers_are_the_files_vigild_ships() -> None:
    py_path, py = helper_file("d", vitest=False)
    js_path, js = helper_file("d", vitest=True)
    assert py_path.endswith(f"{HELPER_NAME}.py") and js_path.endswith(f"{HELPER_NAME}.mjs")
    assert "call_subject" in py and "callSubject" in js
    assert json.dumps(py) and "VIGIL_HIDDEN_TAG" in py + js
