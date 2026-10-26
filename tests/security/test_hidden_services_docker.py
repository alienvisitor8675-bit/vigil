"""Nothing the test uid (10001) runs may accept requests from the subject uid
(10002) during a hidden run (pytest -m docker).

The container has one loopback and 10001 and 10002 share it, so a service started
as 10001 (the suites' database in ParkAgent's dev image, a listener a test opens)
is a service that acts as 10001 for whoever connects. The hidden run therefore
(1) stops what the image's prepare step started, (2) refuses every subject call
while a 10001-owned TCP, UDP or reachable unix listener exists (the broker's
guard, `hiddenkit/guard.sh`), and (3) keeps /work, with the hidden tests in it,
private to 10001.

Run in the repos' own dev images with no network, as the verifier does."""

from __future__ import annotations

from typing import Any

import pytest

from tests.security.test_hidden_fixtures_docker import PARKAGENT_IMAGE, VIGIL_IMAGE, _run

pytestmark = pytest.mark.docker

PY_SUBJECT = {
    "work.py": (
        "import json, os\n"
        "res = {}\n"
        "for label, fn in (('list', lambda: os.listdir('/work')),\n"
        "                  ('read', lambda: open('/work/probe.py').read()),\n"
        "                  ('stat', lambda: os.stat('/work/probe.py'))):\n"
        "    try:\n        fn()\n        res[label] = 'DONE'\n"
        "    except OSError as e:\n        res[label] = type(e).__name__\n"
        "print(json.dumps(res))\n"
    ),
}

PY_PROBE = (
    "import json, os, socket\n"
    "from vigil_hidden import SubjectRefused, call_subject, run_subject\n\n"
    "def refusal(fn):\n"
    "    try:\n        fn()\n        return None\n"
    "    except SubjectRefused as e:\n        return str(e)\n\n"
    "out = {'me': os.getuid(), 'first': run_subject(['echo', 'ok'])['returncode']}\n"
    "r = run_subject(['python3', 'work.py'])\n"
    "out['work'] = json.loads(r['stdout']) if r['returncode'] == 0 else r['stderr'][-300:]\n"
    "s = socket.socket()\n"
    "s.bind(('127.0.0.1', 0))\n"
    "s.listen()\n"
    "out['tcp'] = refusal(lambda: run_subject(['echo', 'x']))\n"
    "s.close()\n"
    "out['after_tcp'] = run_subject(['echo', 'ok'])['returncode']\n"
    "reach = '/vigil-fx/probe.sock'\n"
    "u = socket.socket(socket.AF_UNIX)\n"
    "u.bind(reach)\n"
    "os.chmod(reach, 0o777)\n"
    "u.listen()\n"
    "out['unix'] = refusal(lambda: run_subject(['echo', 'x']))\n"
    "out['unix_call'] = refusal(lambda: call_subject('work', 'x'))\n"
    "u.close()\n"
    "os.unlink(reach)\n"
    "private = os.environ['TMPDIR'] + '/p.sock'  # in a 0700 dir: the subject cannot reach it\n"
    "p = socket.socket(socket.AF_UNIX)\n"
    "p.bind(private)\n"
    "os.chmod(private, 0o777)\n"
    "p.listen()\n"
    "out['private_unix'] = run_subject(['echo', 'ok'])['returncode']\n"
    "p.close()\n"
    "u = socket.socket(socket.AF_UNIX)\n"
    "u.bind('\\0vigil-probe')\n"
    "u.listen()\n"
    "out['abstract'] = refusal(lambda: run_subject(['echo', 'x']))\n"
    "u.close()\n"
    "d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "d.bind(('127.0.0.1', 0))\n"
    "out['udp'] = refusal(lambda: run_subject(['echo', 'x']))\n"
    "d.close()\n"
    "out['last'] = run_subject(['echo', 'ok'])['returncode']\n"
    "print('RESULT ' + json.dumps(out))\n"
)

JS_SUBJECT = {
    "work.mjs": (
        "import fs from 'node:fs'\n"
        "const res = {}\n"
        "const tries = { list: () => fs.readdirSync('/work'),\n"
        "                read: () => fs.readFileSync('/work/probe.mjs', 'utf8'),\n"
        "                stat: () => fs.statSync('/work/probe.mjs') }\n"
        "for (const [k, fn] of Object.entries(tries)) {\n"
        "  try { fn(); res[k] = 'DONE' } catch (e) { res[k] = e.code }\n"
        "}\n"
        "console.log(JSON.stringify(res))\n"
    ),
    "pg.mjs": (
        "import net from 'node:net'\n"
        "const tryConnect = (opts) => new Promise((resolve) => {\n"
        "  const c = net.connect(opts)\n"
        "  c.setTimeout(3000, () => { c.destroy(); resolve('TIMEOUT') })\n"
        "  c.on('connect', () => { c.destroy(); resolve('CONNECTED') })\n"
        "  c.on('error', (e) => resolve(e.code))\n"
        "})\n"
        "const res = {\n"
        "  tcp: await tryConnect({ host: '127.0.0.1', port: 5432 }),\n"
        "  unix: await tryConnect({ path: '/tmp/.s.PGSQL.5432' }),\n"
        "}\n"
        "console.log(JSON.stringify(res))\n"
    ),
}

JS_PROBE = (
    "import net from 'node:net'\n"
    "import { SubjectRefused, runSubject } from './vigil_hidden.mjs'\n\n"
    "const refusal = (fn) => { try { fn(); return null } catch (e) {\n"
    "  if (e instanceof SubjectRefused) return e.message\n  throw e\n} }\n"
    "const out = { me: process.getuid(), first: runSubject(['echo', 'ok']).returncode }\n"
    "const w = runSubject(['node', 'work.mjs'])\n"
    "out.work = w.returncode === 0 ? JSON.parse(w.stdout) : w.stderr.slice(-300)\n"
    "const p = runSubject(['node', 'pg.mjs'])\n"
    "out.pg = p.returncode === 0 ? JSON.parse(p.stdout) : p.stderr.slice(-300)\n"
    "const srv = net.createServer()\n"
    "await new Promise((r) => srv.listen(0, '127.0.0.1', r))\n"
    "out.tcp = refusal(() => runSubject(['echo', 'x']))\n"
    "await new Promise((r) => srv.close(r))\n"
    "out.afterTcp = runSubject(['echo', 'ok']).returncode\n"
    "const ab = net.createServer()\n"
    "await new Promise((r) => ab.listen('\\0vigil-probe', r))\n"
    "out.abstract = refusal(() => runSubject(['echo', 'x']))\n"
    "await new Promise((r) => ab.close(r))\n"
    "out.last = runSubject(['echo', 'ok']).returncode\n"
    "console.log('RESULT ' + JSON.stringify(out))\n"
)


def _refused(reason: Any) -> bool:
    """A refusal came back as a SubjectRefused, never as a returncode."""
    return isinstance(reason, str) and "owned by the test uid" in reason


def _work_closed(work: Any) -> None:
    assert work == {
        "list": "PermissionError",
        "read": "PermissionError",
        "stat": "PermissionError",
    } or work == {
        "list": "EACCES",
        "read": "EACCES",
        "stat": "EACCES",
    }, work


def test_python_image_the_guard_refuses_calls_while_a_10001_service_exists(
    cfg: Any, runtime: object, tmp_path: Any
) -> None:
    got = _run(
        cfg, runtime, tmp_path, VIGIL_IMAGE, vitest=False, probe=("probe.py", PY_PROBE),
        subject_files=PY_SUBJECT, command="python3 probe.py",
    )  # fmt: skip
    assert got["me"] == 10001 and got["first"] == 0
    _work_closed(got["work"])
    assert _refused(got["tcp"]), got["tcp"]
    assert got["after_tcp"] == 0  # the guard is per call: it clears when the service goes
    assert _refused(got["unix"]), got["unix"]
    assert _refused(got["unix_call"]), got["unix_call"]
    assert got["private_unix"] == 0  # a socket the subject cannot reach is not a service for it
    assert _refused(got["abstract"]), got["abstract"]
    assert _refused(got["udp"]), got["udp"]
    assert got["last"] == 0


def test_parkagent_image_the_suites_database_is_not_reachable_and_the_guard_trips(
    cfg: Any, runtime: object, tmp_path: Any
) -> None:
    got = _run(
        cfg, runtime, tmp_path, PARKAGENT_IMAGE, vitest=True, probe=("probe.mjs", JS_PROBE),
        subject_files=JS_SUBJECT, command="node probe.mjs",
    )  # fmt: skip
    assert got["me"] == 10001 and got["first"] == 0
    _work_closed(got["work"])
    # the image's prepare step starts PostgreSQL as 10001; a hidden run must not leave it up
    assert got["pg"]["tcp"] == "ECONNREFUSED", got["pg"]
    assert got["pg"]["unix"] in ("ENOENT", "ECONNREFUSED"), got["pg"]
    assert _refused(got["tcp"]), got["tcp"]
    assert got["afterTcp"] == 0
    assert _refused(got["abstract"]), got["abstract"]
    assert got["last"] == 0
