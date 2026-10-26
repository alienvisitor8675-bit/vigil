"""The subject (uid 10002) cannot plant code the test process (uid 10001) loads
(pytest -m docker).

The container's caches (`PYTHONPYCACHEPREFIX`, `HOME`, `XDG_CACHE_HOME`) and its
temp dir used to be paths under the world-writable /tmp that did not exist yet, so
10002 could create them first: a `.pyc` planted under /tmp/pycache ran as 10001 the
next time it imported that module. PID 1 now makes every such path as 10001, mode
0700, before the broker starts, and points the test's TMPDIR at a private one.

Run in the repos' own dev images with no network, as the verifier does."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.security.test_hidden_fixtures_docker import PARKAGENT_IMAGE, VIGIL_IMAGE, _run

pytestmark = pytest.mark.docker

PRIVATE = ["/tmp/pycache", "/tmp/home", "/tmp/cache", "/tmp/t-10001"]

PY_SUBJECT = {
    "plant.py": (
        "import importlib.util, json, os, py_compile, sys\n\n"
        "res = {}\n"
        "src = importlib.util.find_spec('colorsys').origin\n"
        "sys.pycache_prefix = '/tmp/pycache'\n"
        "pyc = importlib.util.cache_from_source(src)\n"
        "payload = os.path.join(os.environ['TMPDIR'], 'payload.py')\n"
        "open(payload, 'w').write(\"open('/tmp/PWNED-pyc', 'w').write('x')\\n\")\n"
        "try:\n"
        "    py_compile.compile(payload, cfile=pyc, doraise=True,\n"
        "                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)\n"
        "    res['pyc'] = 'PLANTED ' + pyc\n"
        "except (OSError, py_compile.PyCompileError) as e:\n"
        "    res['pyc'] = type(e).__name__\n"
        f"for d in {PRIVATE!r}:\n"
        "    try:\n"
        "        os.makedirs(d + '/x/y', exist_ok=True)\n"
        "        open(d + '/x/y/fake.py', 'w').write('x = 1\\n')\n"
        "        res[d] = 'PLANTED'\n"
        "    except OSError as e:\n"
        "        res[d] = type(e).__name__\n"
        "print(json.dumps(res))\n"
    ),
}

PY_PROBE = (
    "import json, os, stat, tempfile\n"
    "from vigil_hidden import run_subject\n\n"
    "out = {'me': os.getuid(), 'tmpdir': tempfile.gettempdir()}\n"
    "r = run_subject(['python3', 'plant.py'])\n"
    "out['plant'] = json.loads(r['stdout']) if r['returncode'] == 0 else r['stderr'][-500:]\n"
    "import colorsys  # the test process loads a stdlib module the subject planted bytecode for\n"
    "out['pwned'] = os.path.exists('/tmp/PWNED-pyc')\n"
    "out['dirs'] = {}\n"
    f"for d in {PRIVATE!r}:\n"
    "    try:\n"
    "        st = os.stat(d)\n"
    "        out['dirs'][d] = [st.st_uid, stat.S_IMODE(st.st_mode)]\n"
    "    except OSError as e:\n"
    "        out['dirs'][d] = type(e).__name__\n"
    "print('RESULT ' + json.dumps(out))\n"
)

JS_SUBJECT = {
    "plant.mjs": (
        "import fs from 'node:fs'\n"
        "const res = {}\n"
        f"const targets = {json.dumps(PRIVATE)}.concat(['/tmp/node-compile-cache'])\n"
        "for (const d of targets) {\n"
        "  try {\n"
        "    fs.mkdirSync(d + '/x/y', { recursive: true })\n"
        "    fs.writeFileSync(d + '/x/y/fake.js', 'x')\n"
        "    res[d] = 'PLANTED'\n"
        "  } catch (e) { res[d] = e.code }\n"
        "}\n"
        "// a module the test process would find through $HOME, if the subject could make it\n"
        "try {\n"
        "  fs.mkdirSync('/tmp/home/.node_modules/vigil-optional', { recursive: true })\n"
        "  fs.writeFileSync('/tmp/home/.node_modules/vigil-optional/index.js',\n"
        "                   \"require('fs').writeFileSync('/tmp/PWNED-js', 'x')\")\n"
        "  res.module = 'PLANTED'\n"
        "} catch (e) { res.module = e.code }\n"
        "console.log(JSON.stringify(res))\n"
    ),
}

JS_PROBE = (
    "import fs from 'node:fs'\nimport os from 'node:os'\nimport module from 'node:module'\n"
    "import { runSubject } from './vigil_hidden.mjs'\n\n"
    "const out = { me: process.getuid(), tmpdir: os.tmpdir() }\n"
    "const r = runSubject(['node', 'plant.mjs'])\n"
    "out.plant = r.returncode === 0 ? JSON.parse(r.stdout) : r.stderr.slice(-500)\n"
    "out.dirs = {}\n"
    f"for (const d of {json.dumps(PRIVATE)}) {{\n"
    "  try { const s = fs.statSync(d); out.dirs[d] = [s.uid, s.mode & 0o777] } catch (e) { out.dirs[d] = e.code }\n"
    "}\n"
    "import { createRequire } from 'node:module'\n"
    "try { createRequire(import.meta.url)('vigil-optional') } catch (e) { /* not found: fine */ }\n"
    "out.pwned = fs.existsSync('/tmp/PWNED-js')\n"
    "// the compile cache lands under the test's own tmpdir, not a dir the subject made\n"
    "const c = module.enableCompileCache ? module.enableCompileCache() : {}\n"
    "out.cache = c.directory || null\n"
    "console.log('RESULT ' + JSON.stringify(out))\n"
)


def _closed(got: dict[str, Any]) -> None:
    assert got["me"] == 10001
    assert got["tmpdir"] == "/tmp/t-10001", got["tmpdir"]
    for d in PRIVATE:
        assert got["dirs"][d] == [10001, 0o700], (d, got["dirs"][d])
        assert got["plant"][d] in ("PermissionError", "EACCES"), (d, got["plant"])


@pytest.fixture
def py_probe(cfg: Any, runtime: object, tmp_path: Path) -> dict[str, Any]:
    return _run(
        cfg, runtime, tmp_path, VIGIL_IMAGE, vitest=False, probe=("probe.py", PY_PROBE),
        subject_files=PY_SUBJECT, command="python3 probe.py",
    )  # fmt: skip


@pytest.fixture
def js_probe(cfg: Any, runtime: object, tmp_path: Path) -> dict[str, Any]:
    return _run(
        cfg, runtime, tmp_path, PARKAGENT_IMAGE, vitest=True, probe=("probe.mjs", JS_PROBE),
        subject_files=JS_SUBJECT,
        # then the project's own vitest, as a hidden run would
        command="node probe.mjs && cd server && ./node_modules/.bin/vitest run",
        src_files={
            "server/package.json": '{"name": "probe", "type": "module"}\n',
            "server/probe.test.mjs": (
                "import { expect, test } from 'vitest'\n"
                "test('runs', () => { expect(1 + 1).toBe(2) })\n"
            ),
        },
    )  # fmt: skip


def test_python_the_subject_cannot_plant_bytecode_the_test_process_imports(
    py_probe: dict[str, Any],
) -> None:
    assert py_probe["pwned"] is False, "planted bytecode ran as the test process"
    assert str(py_probe["plant"]["pyc"]) in ("PermissionError", "OSError"), py_probe["plant"]
    _closed(py_probe)


def test_javascript_the_subject_cannot_plant_caches_the_test_process_uses(
    js_probe: dict[str, Any],
) -> None:
    assert js_probe["pwned"] is False, "planted code ran as the test process"
    _closed(js_probe)
    assert js_probe["plant"]["module"] in ("EACCES", "ENOENT"), js_probe["plant"]
    cache = js_probe["cache"]
    assert cache is None or cache.startswith("/tmp/t-10001/"), cache
    # the one world-writable path the subject can still make is one nothing reads
    assert js_probe["plant"]["/tmp/node-compile-cache"] == "PLANTED"
