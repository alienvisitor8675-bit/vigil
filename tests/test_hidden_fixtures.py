"""`make_fixture` / `makeFixture`: the one way a hidden test creates a fixture.

In the verifier the subject runs as uid 10002 and the test as 10001, so a fixture
must be readable by 10002 at every level, and never writable by it. These tests
run the helpers for real (no Docker): modes, base directory, path safety, and the
subject reading what the fixture holds. The broker path is
tests/security/test_subject_broker_docker.py."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vigild.verifier import TAG_ENV, helper_file

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

FILES = {"a.txt": "alpha\n", "dir/sub/b.txt": "beta\n", "dir/c.json": '{"c": 1}\n'}


def _lay(tmp_path: Path, vitest: bool) -> Path:
    rel, text = helper_file("h", vitest)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.parent


def _py(tmp_path: Path, script: str, base: Path | None, subject: Path) -> dict[str, Any]:
    helper_dir = _lay(tmp_path, vitest=False)
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "VIGIL_SUBJECT": str(subject),
        TAG_ENV: "TAGGED",
        "PYTHONPATH": str(helper_dir),
    }
    if base is not None:
        env["VIGIL_FIXTURES"] = str(base)
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=helper_dir, env=env,
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    return {"code": out.returncode, "out": out.stdout, "err": out.stderr}


def _node(tmp_path: Path, script: str, base: Path | None, subject: Path) -> dict[str, Any]:
    helper_dir = _lay(tmp_path, vitest=True)
    (helper_dir / "run.mjs").write_text(script)
    env = {
        "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("node") or "/").parent),
        "VIGIL_SUBJECT": str(subject),
        TAG_ENV: "TAGGED",
    }
    if base is not None:
        env["VIGIL_FIXTURES"] = str(base)
    out = subprocess.run(
        [shutil.which("node") or "node", "run.mjs"], cwd=helper_dir, env=env,
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    return {"code": out.returncode, "out": out.stdout, "err": out.stderr}


def _subject(tmp_path: Path) -> Path:
    s = tmp_path / "subject"
    s.mkdir()
    (s / "cat.py").write_text("import sys\nprint(open(sys.argv[1]).read(), end='')\n")
    return s


def _modes(root: Path) -> dict[str, int]:
    return {
        str(p.relative_to(root)) or ".": stat.S_IMODE(p.stat().st_mode)
        for p in [root, *root.rglob("*")]
    }


PY_MAKE = (
    "import json, os\nfrom vigil_hidden import make_fixture\n"
    "os.umask(0o077)\n"  # a hostile umask: the modes must not depend on it
    f"fx = make_fixture({FILES!r})\nprint(json.dumps({{'fx': fx}}))\n"
)


def test_python_make_fixture_modes_and_content_under_the_base(tmp_path: Path) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    got = _py(tmp_path, PY_MAKE, base, _subject(tmp_path))
    assert got["code"] == 0, got
    fx = Path(json.loads(got["out"])["fx"])
    assert fx.parent == base and fx.is_dir()
    assert (fx / "dir/sub/b.txt").read_text() == "beta\n"
    assert (fx / "a.txt").read_text() == "alpha\n"
    modes = _modes(fx)
    for rel, mode in modes.items():
        want = 0o755 if (fx / rel).is_dir() else 0o644
        assert mode == want, (rel, oct(mode))


def test_python_two_fixtures_are_distinct_directories(tmp_path: Path) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    script = (
        "from vigil_hidden import make_fixture\n"
        "a = make_fixture({'x': '1'})\nb = make_fixture({'x': '2'})\n"
        "assert a != b\nprint(open(a + '/x').read(), open(b + '/x').read())\n"
    )
    got = _py(tmp_path, script, base, _subject(tmp_path))
    assert got["code"] == 0 and got["out"].split() == ["1", "2"], got


def test_python_without_a_base_it_makes_one_that_others_can_traverse(tmp_path: Path) -> None:
    got = _py(tmp_path, PY_MAKE, None, _subject(tmp_path))
    assert got["code"] == 0, got
    fx = Path(json.loads(got["out"])["fx"])
    try:
        assert stat.S_IMODE(fx.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(fx.stat().st_mode) == 0o755
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../escape", "a/../../escape", "", "a//b", "./", "a/\0b", "a/.", "dir/"],
)
def test_python_make_fixture_rejects_unsafe_paths(tmp_path: Path, bad: str) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    script = (
        "from vigil_hidden import make_fixture\n"
        f"try:\n    make_fixture({{{bad!r}: 'x'}})\nexcept ValueError as e:\n"
        "    print('REFUSED', e)\n"
    )
    got = _py(tmp_path, script, base, _subject(tmp_path))
    assert got["code"] == 0 and got["out"].startswith("REFUSED unsafe fixture path"), got
    assert not (tmp_path / "escape").exists()
    assert list(base.iterdir()) == [] or all(not any(p.rglob("*")) for p in base.iterdir())


def test_python_make_fixture_rejects_a_file_that_is_also_a_directory(tmp_path: Path) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    script = (
        "from vigil_hidden import make_fixture\n"
        "try:\n    make_fixture({'a': 'x', 'a/b': 'y'})\nexcept ValueError as e:\n"
        "    print('REFUSED', e)\n"
    )
    got = _py(tmp_path, script, base, _subject(tmp_path))
    assert (
        got["code"] == 0 and "REFUSED a fixture path is both a file and a directory" in got["out"]
    ), got


def test_python_make_fixture_refuses_a_symlinked_base_and_never_follows_links(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    script = (
        "from vigil_hidden import make_fixture\n"
        "try:\n    make_fixture({'x': '1'})\nexcept ValueError as e:\n    print('REFUSED', e)\n"
    )
    got = _py(tmp_path, script, link, _subject(tmp_path))
    assert got["code"] == 0 and "is not a real directory" in got["out"], got
    assert list(real.iterdir()) == []


def test_python_the_subject_reads_a_fixture_through_run_subject_and_call_subject(
    tmp_path: Path,
) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    subject = _subject(tmp_path)
    (subject / "lib.py").write_text(
        "def first_line(root):\n    return open(root + '/dir/sub/b.txt').read().strip()\n"
    )
    script = (
        "from vigil_hidden import call_subject, check, make_fixture, run_subject\n"
        f"fx = make_fixture({FILES!r})\n"
        "r = run_subject(['python3', 'cat.py', fx + '/dir/sub/b.txt'])\n"
        "check(r['returncode'] == 0 and r['stdout'] == 'beta\\n', repr(r))\n"
        "c = call_subject('lib', 'first_line', args=[fx])\n"
        "check(c == {'ok': True, 'value': 'beta'}, repr(c))\nprint('PASSED')\n"
    )
    got = _py(tmp_path, script, base, subject)
    assert got["code"] == 0 and "PASSED" in got["out"], got


JS_MAKE = (
    "import { makeFixture } from './vigil_hidden.mjs'\nimport fs from 'node:fs'\n"
    "process.umask(0o077)\n"
    f"const fx = makeFixture({json.dumps(FILES)})\n"
    "const modes = {}\n"
    "const walk = (d, rel) => {\n"
    "  modes[rel || '.'] = fs.statSync(d).mode & 0o777\n"
    "  if (fs.statSync(d).isDirectory()) for (const n of fs.readdirSync(d)) walk(d + '/' + n, rel ? rel + '/' + n : n)\n"
    "}\nwalk(fx, '')\n"
    "console.log(JSON.stringify({ fx, modes }))\n"
)


@needs_node
def test_javascript_make_fixture_modes_and_content_under_the_base(tmp_path: Path) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    got = _node(tmp_path, JS_MAKE, base, _subject(tmp_path))
    assert got["code"] == 0, got
    out = json.loads(got["out"])
    fx = Path(out["fx"])
    assert fx.parent == base
    assert (fx / "dir/sub/b.txt").read_text() == "beta\n"
    for rel, mode in out["modes"].items():
        want = 0o755 if (fx / rel).is_dir() else 0o644
        assert mode == want, (rel, oct(mode))


@needs_node
@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "a/../../escape", "", "a//b", "a/."])
def test_javascript_make_fixture_rejects_unsafe_paths(tmp_path: Path, bad: str) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    script = (
        "import { makeFixture } from './vigil_hidden.mjs'\n"
        f"try {{ makeFixture({{ [{json.dumps(bad)}]: 'x' }}) }} catch (e) {{ console.log('REFUSED ' + e.message) }}\n"
    )
    got = _node(tmp_path, script, base, _subject(tmp_path))
    assert got["code"] == 0 and got["out"].startswith("REFUSED unsafe fixture path"), got
    assert not (tmp_path / "escape").exists()


@needs_node
def test_javascript_the_subject_reads_a_fixture_through_run_and_call_subject(
    tmp_path: Path,
) -> None:
    base = tmp_path / "fx"
    base.mkdir(mode=0o755)
    subject = _subject(tmp_path)
    (subject / "lib.mjs").write_text(
        "import fs from 'node:fs'\n"
        "export function firstLine(root) { return fs.readFileSync(root + '/dir/sub/b.txt', 'utf8').trim() }\n"
    )
    script = (
        "import { callSubject, check, makeFixture, runSubject } from './vigil_hidden.mjs'\n"
        f"const fx = makeFixture({json.dumps(FILES)})\n"
        "const r = runSubject(['python3', 'cat.py', fx + '/dir/sub/b.txt'])\n"
        "check(r.returncode === 0 && r.stdout === 'beta\\n', JSON.stringify(r))\n"
        "const c = callSubject('lib.mjs', 'firstLine', [fx])\n"
        "check(c.ok && c.value === 'beta', JSON.stringify(c))\nconsole.log('PASSED')\n"
    )
    got = _node(tmp_path, script, base, subject)
    assert got["code"] == 0 and "PASSED" in got["out"], got


@needs_node
def test_javascript_make_fixture_refuses_a_symlinked_base(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    script = (
        "import { makeFixture } from './vigil_hidden.mjs'\n"
        "try { makeFixture({ x: '1' }) } catch (e) { console.log('REFUSED ' + e.message) }\n"
    )
    got = _node(tmp_path, script, link, _subject(tmp_path))
    assert got["code"] == 0 and "is not a real directory" in got["out"], got
    assert list(real.iterdir()) == []


# --- a broker refusal is not a subject exit ------------------------------------------------------

FAKE_BROKER = r"""
import os, sys, time
ipc, mode = sys.argv[1], sys.argv[2]
fd = os.open(ipc + "/queue", os.O_RDWR)
buf = b""
while b"\n" not in buf:
    buf += os.read(fd, 100)
d = ipc + "/" + buf.split(b"\n")[0].decode()
for name in ("out", "fd3", "started"):
    open(d + "/" + name, "w").close()
if mode == "refuse":
    open(d + "/err", "w").write("a service owned by the test uid is reachable by the subject\n")
    open(d + "/refused", "w").write("1\n")
    open(d + "/rc", "w").write("126\n")
else:  # a subject that itself exits 126
    open(d + "/err", "w").write("the subject's own stderr\n")
    open(d + "/rc", "w").write("126\n")
"""


def _fake_broker(tmp_path: Path, mode: str) -> tuple[Path, subprocess.Popen[bytes]]:
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    import os

    os.mkfifo(ipc / "queue")
    script = tmp_path / "broker.py"
    script.write_text(FAKE_BROKER)
    return ipc, subprocess.Popen([sys.executable, str(script), str(ipc), mode])


def _run_py_with_broker(tmp_path: Path, ipc: Path, body: str) -> dict[str, Any]:
    helper_dir = _lay(tmp_path, vitest=False)
    kit = Path(__file__).parents[1] / "vigild" / "hiddenkit"
    run_dir = tmp_path / "vigil"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "run.sh").write_text((kit / "run.sh").read_text())
    subject = _subject(tmp_path)
    script = f"from vigil_hidden import *\n{body}"
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=helper_dir, capture_output=True, text=True, timeout=120,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "VIGIL_SUBJECT": str(subject),
             "VIGIL_BROKER": str(ipc), TAG_ENV: "T", "PYTHONPATH": str(helper_dir),
             "VIGIL_RUN_SH": str(run_dir / "run.sh")},
    )  # fmt: skip
    return {"code": out.returncode, "out": out.stdout, "err": out.stderr}


def test_python_a_broker_refusal_raises_and_is_not_a_subject_exit(tmp_path: Path) -> None:
    ipc, proc = _fake_broker(tmp_path, "refuse")
    body = (
        "try:\n    r = run_subject(['echo', 'x'])\n    print('RETURNED', r)\n"
        "except SubjectRefused as e:\n    print('REFUSED', e)\n"
    )
    got = _run_py_with_broker(tmp_path, ipc, body)
    proc.wait(timeout=30)
    assert "REFUSED a service owned by the test uid" in got["out"], got


def test_python_a_subject_that_exits_126_is_a_normal_result(tmp_path: Path) -> None:
    ipc, proc = _fake_broker(tmp_path, "exit126")
    body = (
        "r = run_subject(['echo', 'x'])\nprint('RETURNED', r['returncode'], r['stderr'].strip())\n"
    )
    got = _run_py_with_broker(tmp_path, ipc, body)
    proc.wait(timeout=30)
    assert "RETURNED 126 the subject's own stderr" in got["out"], got


@needs_node
def test_javascript_a_broker_refusal_throws_and_is_not_a_subject_exit(tmp_path: Path) -> None:
    for mode, want in (("refuse", "REFUSED"), ("exit126", "RETURNED 126")):
        work = tmp_path / mode
        work.mkdir()
        ipc, proc = _fake_broker(work, mode)
        helper_dir = _lay(work, vitest=True)
        shutil.copy(Path(__file__).parents[1] / "vigild" / "hiddenkit" / "run.sh", work / "run.sh")
        (helper_dir / "run.mjs").write_text(
            "import { runSubject } from './vigil_hidden.mjs'\n"
            "try { const r = runSubject(['echo', 'x']); console.log('RETURNED', r.returncode) }\n"
            "catch (e) { console.log('REFUSED', e.name, e.message) }\n"
        )
        out = subprocess.run(
            [shutil.which("node") or "node", "run.mjs"], cwd=helper_dir, capture_output=True,
            text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin:" + str(Path(shutil.which("node") or "/").parent),
                 "VIGIL_SUBJECT": str(_subject(work)), "VIGIL_BROKER": str(ipc), TAG_ENV: "T",
                 "VIGIL_RUN_SH": str(work / "run.sh")},
        )  # fmt: skip
        proc.wait(timeout=30)
        assert want in out.stdout, (mode, out.stdout, out.stderr)
        if mode == "refuse":
            assert "SubjectRefused" in out.stdout and "owned by the test uid" in out.stdout
