"""The subject broker, for real (pytest -m docker; hotfix 5, round 2).

A hidden run's container starts as root with only the capabilities it needs,
starts vigild's broker, and drops the test command to uid 10001. Every subject
(`run_subject`, `call_subject`) then runs as uid 10002 in its own process group
through the broker, which kills the group (and every process of 10002) when the
call returns. The report dir and the broker's queue belong to 10001, mode 0700.

The probe below is the hidden test process; the subjects are hostile."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vigild.config import Config
from vigild.executors.runtime import DockerTestRunner
from vigild.verifier import helper_file

pytestmark = pytest.mark.docker

SUBJECT_FILES = {
    "who.py": "import os\nprint(os.getuid(), os.getgid(), os.getgroups())\n",
    "whoami.py": "import os\n\n\ndef uid():\n    return os.getuid()\n",
    # forks a daemon (double fork, own session) that rewrites the report 2s later
    "daemon.py": (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        open('/tmp/daemon-started', 'w').write('yes')\n"
        "        time.sleep(2)\n"
        "        for p in ('/tmp/vigil-report/pytest.xml', '/tmp/vigil-report/FORGED'):\n"
        "            try:\n"
        "                open(p, 'w').write('FORGED')\n"
        "            except OSError:\n"
        "                pass\n"
        "        open('/tmp/survived', 'w').write('yes')\n"
        "        os._exit(0)\n"
        "    os._exit(0)\n"
    ),
    "calld.py": (
        "import os, time\n\n\ndef spawn():\n"
        "    if os.fork() == 0:\n"
        "        os.setsid()\n"
        "        time.sleep(2)\n"
        "        open('/tmp/survived-call', 'w').write('yes')\n"
        "        os._exit(0)\n"
        "    return 1\n"
    ),
    "sig.py": (
        "import ctypes, json, os, sys\n"
        "pid = int(sys.argv[1])\nres = {}\n"
        "for name, target in (('test', pid), ('init', 1)):\n"
        "    try:\n        os.kill(target, 0)\n        res['kill_' + name] = 'allowed'\n"
        "    except PermissionError:\n        res['kill_' + name] = 'EPERM'\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "res['ptrace'] = libc.ptrace(16, pid, 0, 0)\n"
        "res['errno'] = ctypes.get_errno()\n"
        "try:\n    open(f'/proc/{pid}/environ').read()\n    res['environ'] = 'readable'\n"
        "except OSError:\n    res['environ'] = 'denied'\n"
        "print(json.dumps(res))\n"
    ),
    "writes.py": (
        "import json\nres = {}\n"
        "for p in ('/tmp/vigil-report/x', '/subject/x', '/work/x', '/vigil-ipc/x', '/vigil/x'):\n"
        "    try:\n        open(p, 'w').close()\n        res[p] = 'written'\n"
        "    except OSError as e:\n        res[p] = type(e).__name__\n"
        "try:\n    import os\n    res['tmp'] = os.access(os.environ['TMPDIR'], os.W_OK)\n"
        "except Exception as e:\n    res['tmp'] = repr(e)\n"
        "print(json.dumps(res))\n"
    ),
    "env.py": "import json, os\nprint(json.dumps(sorted(os.environ)))\n",
}

PROBE = r"""
import faulthandler, json, os, time
faulthandler.dump_traceback_later(150, exit=True)  # a hang shows where, instead of waiting
from vigil_hidden import call_subject, run_subject

out = {"me": os.getuid()}
me = os.getpid()
r = run_subject(["python3", "who.py"])
out["who"] = r["stdout"].split()[:2]
out["groups"] = r["stdout"].split("[", 1)[1]
out["call_uid"] = call_subject("whoami", "uid")
out["env"] = json.loads(run_subject(["python3", "env.py"])["stdout"])
out["sig"] = json.loads(run_subject(["python3", "sig.py", str(me)])["stdout"])
out["writes"] = json.loads(run_subject(["python3", "writes.py"])["stdout"])

run_subject(["python3", "daemon.py"])
call_subject("calld", "spawn")
time.sleep(4)
out["daemon_started"] = os.path.exists("/tmp/daemon-started")
out["forged"] = any(os.path.exists(p) for p in ("/tmp/vigil-report/FORGED", "/tmp/survived", "/tmp/survived-call"))
out["report_dir"] = sorted(os.listdir("/tmp/vigil-report"))
out["procs_10002"] = [
    p for p in os.listdir("/proc") if p.isdigit() and os.stat(f"/proc/{p}").st_uid == 10002
]

# the test process (10001) can't read the subject at all, by any route
unreadable = {}
def attempt(label, fn):
    try:
        fn()
        unreadable[label] = "READ"
    except PermissionError:
        unreadable[label] = "EACCES"
    except OSError as e:
        unreadable[label] = type(e).__name__
attempt("listdir", lambda: os.listdir("/subject"))
attempt("open", lambda: open("/subject/whoami.py").read())
attempt("source mount", lambda: os.listdir("/vigil-src"))
attempt("source file", lambda: open("/vigil-src/subject/whoami.py").read())
import sys
sys.path.insert(0, "/subject")
try:
    import whoami
    unreadable["import"] = "IMPORTED"
except ImportError:
    unreadable["import"] = "ImportError"
out["unreadable"] = unreadable
# four parallel calls all get served, each timed from when it starts
import threading
codes = []
t0 = time.time()
ts = [threading.Thread(target=lambda: codes.append(run_subject(["sleep", "1"], timeout=3)["returncode"])) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
out["parallel"] = codes
out["parallel_secs"] = round(time.time() - t0, 1)

# the broker refuses what is not a well-formed run spec
refused = {}
for label, files in {
    "cwd outside the subject": {"cwd": "/etc\n", "timeout": "5\n", "argv": "id\n"},
    "cwd escapes with ..": {"cwd": "/subject/..\n", "timeout": "5\n", "argv": "id\n"},
    "bad timeout": {"cwd": "/subject\n", "timeout": "abc\n", "argv": "id\n"},
    "huge timeout": {"cwd": "/subject\n", "timeout": "99999\n", "argv": "id\n"},
    "no argv": {"cwd": "/subject\n", "timeout": "5\n"},
    "empty argv": {"cwd": "/subject\n", "timeout": "5\n", "argv": "\n"},
    "bad env": {"cwd": "/subject\n", "timeout": "5\n", "argv": "id\n", "env": "not an assignment\n"},
}.items():
    name = "req-" + os.urandom(16).hex()
    d = "/vigil-ipc/" + name
    os.mkdir(d, 0o700)
    for fname, text in files.items():
        open(f"{d}/{fname}", "w").write(text)
    with open("/vigil-ipc/queue", "w") as q:
        q.write(name + "\n")
    for _ in range(200):
        if os.path.exists(d + "/rc"):
            break
        time.sleep(0.05)
    refused[label] = [open(d + "/rc").read().strip() if os.path.exists(d + "/rc") else None,
                      open(d + "/err").read().strip() if os.path.exists(d + "/err") else None]
out["refused"] = refused
# junk in the queue is ignored, and the broker still answers afterwards
with open("/vigil-ipc/queue", "w") as q:
    q.write("../../etc\nreq-xyz\n\n")
out["still_serving"] = run_subject(["echo", "alive"])["stdout"].strip()
print("RESULT " + json.dumps(out))
"""


@pytest.fixture
def probe(cfg: Config, runtime: object, tmp_path: Path) -> dict[str, Any]:
    runner = DockerTestRunner(cfg, runtime.client)  # type: ignore[attr-defined]
    src = tmp_path / "verify" / "fx-0001-w1-hbase" / "src"
    src.mkdir(parents=True)
    rel, text = helper_file(".", vitest=False)
    (src / rel).write_text(text)
    (src / "probe.py").write_text(PROBE)
    subject = tmp_path / "verify" / "fx-0001-w1-hbase-sub" / "src"
    subject.mkdir(parents=True)
    for name, body in SUBJECT_FILES.items():
        (subject / name).write_text(body)
    # a checkout is readable by every uid, as vigild's is
    for d in (src, subject):
        d.chmod(0o755)
        for f in d.iterdir():
            f.chmod(0o644)
    r = runner.run(
        cfg.vigild.images.claude_executor,
        src,
        "python3 probe.py",
        timeout_s=240,
        max_output=1 << 20,
        env={"VIGIL_HIDDEN_TAG": "TAG-ONLY-THE-TEST-HAS"},
        subject=subject,
    )
    assert r.exit == 0 and "RESULT " in r.output, r.output[-3000:]
    return json.loads(r.output.split("RESULT ", 1)[1].splitlines()[0])  # type: ignore[no-any-return]


def test_every_subject_runs_as_another_uid_without_groups(probe: dict[str, Any]) -> None:
    assert probe["me"] == 10001
    assert probe["who"] == ["10002", "10002"] and probe["groups"].strip() == "]"
    assert probe["call_uid"] == {"ok": True, "value": 10002}


def test_a_forked_background_process_cannot_rewrite_the_report_and_is_gone(
    probe: dict[str, Any],
) -> None:
    assert probe["daemon_started"] is True  # the control: the daemon did run for a moment
    assert probe["forged"] is False
    assert "FORGED" not in probe["report_dir"] and "pytest.xml" not in probe["report_dir"]
    assert probe["procs_10002"] == []


def test_a_subject_cannot_write_the_report_dir_or_anything_but_its_tmp(
    probe: dict[str, Any],
) -> None:
    w = probe["writes"]
    for path in ("/tmp/vigil-report/x", "/subject/x", "/work/x", "/vigil-ipc/x", "/vigil/x"):
        assert w[path] in ("PermissionError", "OSError", "FileNotFoundError"), (path, w)
    assert w["tmp"] is True  # its own fresh tmp dir is writable


def test_a_subject_cannot_signal_or_ptrace_the_test_process_or_init(probe: dict[str, Any]) -> None:
    s = probe["sig"]
    assert s["kill_test"] == "EPERM" and s["kill_init"] == "EPERM"
    assert s["ptrace"] == -1 and s["errno"] in (1, 3)  # EPERM (or ESRCH under a pid namespace)
    assert s["environ"] == "denied"


def test_a_subject_does_not_see_the_tag_or_the_brokers_environment(probe: dict[str, Any]) -> None:
    env = probe["env"]
    assert "VIGIL_HIDDEN_TAG" not in env and "VIGIL_BROKER" not in env
    assert "VIGIL_SUBJ_ENV" not in env


def test_the_broker_refuses_anything_that_is_not_a_well_formed_run_spec(
    probe: dict[str, Any],
) -> None:
    for label, (rc, err) in probe["refused"].items():
        assert rc == "126" and err, (label, rc, err)
    assert probe["still_serving"] == "alive"


def test_the_test_process_cannot_read_the_subject_by_any_route(probe: dict[str, Any]) -> None:
    got = probe["unreadable"]
    assert got["listdir"] == "EACCES" and got["open"] == "EACCES", got
    assert got["source mount"] == "EACCES" and got["source file"] == "EACCES", got
    assert got["import"] == "ImportError", got
    # and run_subject / call_subject still work (the other tests in this file)
    assert probe["call_uid"] == {"ok": True, "value": 10002}


def test_parallel_calls_are_all_served_and_timed_from_their_start(probe: dict[str, Any]) -> None:
    assert probe["parallel"] == [0, 0, 0, 0]
    assert probe["parallel_secs"] >= 3.5  # served one at a time, none timed out waiting


# --- the real JS path, in ParkAgent's dev image ------------------------------------------------

PARKAGENT_IMAGE = "vigil-dev/parkagent:95954ce4209700a1"

JS_PROBE = r"""
import fs from 'node:fs'
import { callSubject, runSubject } from './vigil_hidden.mjs'
const out = { me: process.getuid() }
out.who = runSubject(['id', '-u']).stdout.trim()
out.call = callSubject('lib.mjs', 'uid')
out.env = runSubject(['env']).stdout.includes('VIGIL_HIDDEN_TAG')
const attempt = (label, fn) => { try { fn(); out[label] = 'READ' } catch (e) { out[label] = e.code || e.name } }
attempt('listdir', () => fs.readdirSync('/subject'))
attempt('open', () => fs.readFileSync('/subject/lib.mjs', 'utf8'))
try { await import('/subject/lib.mjs'); out.import = 'IMPORTED' } catch (e) { out.import = e.code || e.name }
const trap = (f) => { try { f(); return 'returned' } catch (e) { return e.message } }
out.hung = trap(() => callSubject('hang.mjs', 'hang', [], { timeout: 2 }))
out.busy = trap(() => callSubject('busy.mjs', 'spin', [], { timeout: 2 }))
out.fake = trap(() => { const r = callSubject('fake.mjs', 'double', [21]); if (r.value !== 42) throw new Error('wrong ' + JSON.stringify(r)); })
console.log('RESULT ' + JSON.stringify(out))
"""
JS_SUBJECT = {
    "lib.mjs": "export function uid() { return process.getuid() }\n",
    "hang.mjs": "export function hang() { return new Promise(() => {}) }\n",
    "busy.mjs": "export function spin() { for (;;) {} }\n",
    "fake.mjs": (
        "console.log('deadbeef ' + JSON.stringify({results: [{ok: true, value: 42}], malformed: false}))\n"
        "export function double(x) { return x + 2 }\n"
    ),
}


def test_the_js_helper_works_through_the_broker_in_parkagents_image(
    cfg: Config, runtime: object, tmp_path: Path
) -> None:
    client = runtime.client  # type: ignore[attr-defined]
    try:
        client.images.get(PARKAGENT_IMAGE)
    except Exception:
        pytest.skip(f"{PARKAGENT_IMAGE} is not built")
    src = tmp_path / "verify" / "pa-0001-w1-hbase" / "src"
    src.mkdir(parents=True)
    rel, text = helper_file(".", vitest=True)
    (src / rel).write_text(text)
    (src / "probe.mjs").write_text(JS_PROBE)
    subject = tmp_path / "verify" / "pa-0001-w1-hbase-sub" / "src"
    subject.mkdir(parents=True)
    for name, body in JS_SUBJECT.items():
        (subject / name).write_text(body.replace("\\n", "\n"))
    for d in (src, subject):
        d.chmod(0o755)
        for f in d.iterdir():
            f.chmod(0o644)
    r = DockerTestRunner(cfg, client).run(
        PARKAGENT_IMAGE,
        src,
        "node probe.mjs",
        timeout_s=240,
        max_output=1 << 20,
        env={"VIGIL_HIDDEN_TAG": "TAG-ONLY-THE-TEST-HAS"},
        subject=subject,
    )
    assert r.exit == 0 and "RESULT " in r.output, r.output[-3000:]
    got = json.loads(r.output.split("RESULT ", 1)[1].splitlines()[0])
    assert got["me"] == 10001 and got["who"] == "10002"
    assert got["call"] == {"ok": True, "value": 10002} and got["env"] is False
    assert got["listdir"] == "EACCES" and got["open"] == "EACCES" and got["import"] != "IMPORTED"
    assert "timed out" in got["hung"] and "malformed" not in got["hung"], got["hung"]
    assert "timed out" in got["busy"], got["busy"]
    assert got["fake"].startswith("wrong") and '"value":23' in got["fake"], got[
        "fake"
    ]  # no fake frame
