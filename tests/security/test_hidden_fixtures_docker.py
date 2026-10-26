"""Hidden-test fixtures through the real broker (pytest -m docker).

`make_fixture` (Python) and `makeFixture` (vitest) build a fixture the subject,
uid 10002, can read at every level and cannot write, under /vigil-fx, which the
hidden run's PID 1 makes as root for the test's uid, mode 0755, before it drops
privileges. Run in the repos' own dev images with no network, as the verifier does:
the Python helper in vigil's, the JavaScript helper in ParkAgent's."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vigild.config import Config
from vigild.executors.runtime import DockerTestRunner
from vigild.verifier import helper_file

pytestmark = pytest.mark.docker

VIGIL_IMAGE = "vigil-dev/vigil:4906134555e113bf"
PARKAGENT_IMAGE = "vigil-dev/parkagent:95954ce4209700a1"

FIXTURE = {
    "top.txt": "top\n",
    "data/nested/a.txt": "hello from a nested file\n",
    "data/nested/deeper/b.txt": "deep\n",
}

PY_SUBJECT = {
    "reader.py": (
        "import json, os, sys\n\n\n"
        "def read_nested(root):\n"
        "    return open(root + '/data/nested/a.txt').read()\n\n\n"
        "def attempts(root):\n"
        "    base = os.path.dirname(root)\n"
        "    tries = {\n"
        "        'overwrite': lambda: open(root + '/data/nested/a.txt', 'w'),\n"
        "        'append': lambda: open(root + '/top.txt', 'a'),\n"
        "        'new file in nested dir': lambda: open(root + '/data/nested/new', 'w'),\n"
        "        'new file in the fixture': lambda: open(root + '/new', 'w'),\n"
        "        'mkdir': lambda: os.mkdir(root + '/d'),\n"
        "        'unlink': lambda: os.unlink(root + '/top.txt'),\n"
        "        'rename': lambda: os.rename(root + '/top.txt', root + '/moved'),\n"
        "        'chmod': lambda: os.chmod(root + '/top.txt', 0o777),\n"
        "        'new file in the base': lambda: open(base + '/new', 'w'),\n"
        "        'mkdir in the base': lambda: os.mkdir(base + '/d'),\n"
        "        'list the base': lambda: os.listdir(base),\n"
        "    }\n"
        "    out = {}\n"
        "    for label, fn in tries.items():\n"
        "        try:\n            fn()\n            out[label] = 'DONE'\n"
        "        except OSError as e:\n            out[label] = type(e).__name__\n"
        "    return out\n\n\n"
        "if __name__ == '__main__':\n"
        "    root = sys.argv[sys.argv.index('--root') + 1]\n"
        "    print(json.dumps({'nested': read_nested(root), 'uid': os.getuid(),\n"
        "                      'attempts': attempts(root)}))\n"
    ),
}

PY_PROBE = (
    "import json, os, stat\n"
    "from vigil_hidden import call_subject, make_fixture, run_subject\n\n"
    f"fx = make_fixture({FIXTURE!r})\n"
    "base = os.path.dirname(fx)\n"
    "out = {'me': os.getuid(), 'base': base, 'fx': fx,\n"
    "       'base_env': os.environ.get('VIGIL_FIXTURES')}\n"
    "st = os.stat(base)\n"
    "out['base_owner'], out['base_mode'] = st.st_uid, stat.S_IMODE(st.st_mode)\n"
    "out['modes'] = {}\n"
    "for d, _, names in os.walk(fx):\n"
    "    out['modes'][d] = [stat.S_IMODE(os.stat(d).st_mode), os.stat(d).st_uid]\n"
    "    for n in names:\n"
    "        p = os.path.join(d, n)\n"
    "        out['modes'][p] = [stat.S_IMODE(os.stat(p).st_mode), os.stat(p).st_uid]\n"
    "r = run_subject(['python3', 'reader.py', '--root', fx])\n"
    "out['run'] = {'returncode': r['returncode'], 'stderr': r['stderr'][-500:],\n"
    "              'data': json.loads(r['stdout']) if r['returncode'] == 0 else None}\n"
    "out['call_read'] = call_subject('reader', 'read_nested', args=[fx])\n"
    "out['call_attempts'] = call_subject('reader', 'attempts', args=[fx])\n"
    "# the fixture is untouched, and the test (its owner) still writes in the base\n"
    "out['after'] = open(fx + '/data/nested/a.txt').read()\n"
    "open(base + '/mine', 'w').close()\n"
    "print('RESULT ' + json.dumps(out))\n"
)

JS_SUBJECT = {
    "reader.mjs": (
        "import fs from 'node:fs'\nimport path from 'node:path'\n\n"
        "export function readNested(root) {\n"
        "  return fs.readFileSync(root + '/data/nested/a.txt', 'utf8')\n}\n\n"
        "export function attempts(root) {\n"
        "  const base = path.dirname(root)\n"
        "  const tries = {\n"
        "    overwrite: () => fs.writeFileSync(root + '/data/nested/a.txt', 'x'),\n"
        "    append: () => fs.appendFileSync(root + '/top.txt', 'x'),\n"
        "    'new file in nested dir': () => fs.writeFileSync(root + '/data/nested/new', 'x'),\n"
        "    'new file in the fixture': () => fs.writeFileSync(root + '/new', 'x'),\n"
        "    mkdir: () => fs.mkdirSync(root + '/d'),\n"
        "    unlink: () => fs.unlinkSync(root + '/top.txt'),\n"
        "    'new file in the base': () => fs.writeFileSync(base + '/new', 'x'),\n"
        "    'list the base': () => fs.readdirSync(base),\n"
        "  }\n"
        "  const out = {}\n"
        "  for (const [label, fn] of Object.entries(tries)) {\n"
        "    try { fn(); out[label] = 'DONE' } catch (e) { out[label] = e.code }\n"
        "  }\n"
        "  return out\n}\n\n"
        "if (process.argv[2] === '--root') {\n"
        "  const root = process.argv[3]\n"
        "  console.log(JSON.stringify({ nested: readNested(root), uid: process.getuid(), attempts: attempts(root) }))\n"
        "}\n"
    ),
}

JS_PROBE = (
    "import fs from 'node:fs'\nimport path from 'node:path'\n"
    "import { callSubject, makeFixture, runSubject } from './vigil_hidden.mjs'\n\n"
    f"const fx = makeFixture({json.dumps(FIXTURE)})\n"
    "const base = path.dirname(fx)\n"
    "const out = { me: process.getuid(), base, fx, baseEnv: process.env.VIGIL_FIXTURES }\n"
    "const st = fs.statSync(base)\n"
    "out.baseOwner = st.uid\nout.baseMode = st.mode & 0o777\n"
    "out.modes = {}\n"
    "const walk = (d) => {\n"
    "  const s = fs.statSync(d)\n"
    "  out.modes[d] = [s.mode & 0o777, s.uid]\n"
    "  if (s.isDirectory()) for (const n of fs.readdirSync(d)) walk(d + '/' + n)\n"
    "}\nwalk(fx)\n"
    "const r = runSubject(['node', 'reader.mjs', '--root', fx])\n"
    "out.run = { returncode: r.returncode, stderr: r.stderr.slice(-500),\n"
    "            data: r.returncode === 0 ? JSON.parse(r.stdout) : null }\n"
    "out.callRead = callSubject('reader.mjs', 'readNested', [fx])\n"
    "out.callAttempts = callSubject('reader.mjs', 'attempts', [fx])\n"
    "out.after = fs.readFileSync(fx + '/data/nested/a.txt', 'utf8')\n"
    "fs.writeFileSync(base + '/mine', '')\n"
    "console.log('RESULT ' + JSON.stringify(out))\n"
)


def _run(
    cfg: Config,
    runtime: object,
    tmp_path: Path,
    image: str,
    *,
    vitest: bool,
    probe: tuple[str, str],
    subject_files: dict[str, str],
    command: str,
    src_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = runtime.client  # type: ignore[attr-defined]
    try:
        client.images.get(image)
    except Exception:
        pytest.skip(f"{image} is not built")
    src = tmp_path / "verify" / "fx-0001-w1-hbase" / "src"
    src.mkdir(parents=True)
    rel, text = helper_file(".", vitest=vitest)
    (src / rel).write_text(text)
    (src / probe[0]).write_text(probe[1])
    for name, body in (src_files or {}).items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(body)
    subject = tmp_path / "verify" / "fx-0001-w1-hbase-sub" / "src"
    subject.mkdir(parents=True)
    for name, body in subject_files.items():
        (subject / name).write_text(body)
    for d in (src, subject):  # a checkout is readable by every uid, as vigild's is
        d.chmod(0o755)
        for f in d.rglob("*"):
            f.chmod(0o755 if f.is_dir() else 0o644)
    r = DockerTestRunner(cfg, client).run(
        image,
        src,
        command,
        timeout_s=240,
        max_output=1 << 20,
        env={"VIGIL_HIDDEN_TAG": "TAG-ONLY-THE-TEST-HAS"},
        subject=subject,
    )
    assert r.exit == 0 and "RESULT " in r.output, r.output[-3000:]
    return json.loads(r.output.split("RESULT ", 1)[1].splitlines()[0])  # type: ignore[no-any-return]


@pytest.fixture
def py_probe(cfg: Config, runtime: object, tmp_path: Path) -> dict[str, Any]:
    return _run(
        cfg, runtime, tmp_path, VIGIL_IMAGE, vitest=False, probe=("probe.py", PY_PROBE),
        subject_files=PY_SUBJECT, command="python3 probe.py",
    )  # fmt: skip


@pytest.fixture
def js_probe(cfg: Config, runtime: object, tmp_path: Path) -> dict[str, Any]:
    return _run(
        cfg, runtime, tmp_path, PARKAGENT_IMAGE, vitest=True, probe=("probe.mjs", JS_PROBE),
        subject_files=JS_SUBJECT, command="node probe.mjs",
    )  # fmt: skip


def _check(
    got: dict[str, Any], *, calls: tuple[str, str], base_owner: str, base_mode: str, base_env: str
) -> None:
    assert got["me"] == 10001
    assert got["base"] == "/vigil-fx" == got[base_env] and got["fx"].startswith("/vigil-fx/")
    # the base: the test's uid owns it, mode 0711; so 10002 traverses it, lists nothing and writes nothing
    assert got[base_owner] == 10001 and got[base_mode] == 0o711
    for path, (mode, owner) in got["modes"].items():
        assert owner == 10001, path
        assert mode in (0o755, 0o644), (path, oct(mode))
    assert sorted(got["modes"]) and any(m[0] == 0o644 for m in got["modes"].values())
    # the subject, as 10002, reads a nested file through --root ...
    assert got["run"]["returncode"] == 0, got["run"]
    data = got["run"]["data"]
    assert data["uid"] == 10002 and data["nested"] == "hello from a nested file\n"
    # ... and through call_subject
    assert got[calls[0]] == {"ok": True, "value": "hello from a nested file\n"}
    # and it can't write: not the file, not a new file, not the fixture's dirs, not the base
    for attempts in (data["attempts"], got[calls[1]]["value"]):
        assert attempts and all(
            v in ("PermissionError", "EACCES", "EROFS") for v in attempts.values()
        ), attempts
    assert got["after"] == "hello from a nested file\n"


def test_python_fixture_is_readable_and_unwritable_by_the_subject_in_the_vigil_image(
    py_probe: dict[str, Any],
) -> None:
    _check(
        py_probe, calls=("call_read", "call_attempts"), base_owner="base_owner",
        base_mode="base_mode", base_env="base_env",
    )  # fmt: skip


def test_javascript_fixture_is_readable_and_unwritable_by_the_subject_in_parkagents_image(
    js_probe: dict[str, Any],
) -> None:
    _check(
        js_probe, calls=("callRead", "callAttempts"), base_owner="baseOwner",
        base_mode="baseMode", base_env="baseEnv",
    )  # fmt: skip
