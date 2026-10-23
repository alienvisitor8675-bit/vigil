"""Phase 2a's done-when, on the test repo only (pytest -m "docker and live").

A uv project in a local bare repo stands in for GitHub ("the test repo");
vigild publishes to it from the host. Workers are Haiku; reviews are Sonnet
and audits Opus, as the review ladder has them. Nothing touches ParkAgent.

1. Four batch tasks run through the scheduler, three at once (the peak is
   asserted), and are reviewed by Sonnet and audited by Opus at rung 0, by
   model id (one of them is sent back once by the Stop hook).
2. The WIP breaker (the production limit, 4) trips on a fifth, pages once,
   pauses dispatch, and is reset from the inbox after a merge.
3. That job's worker is killed mid-task and resumes from its last commit,
   within what was left of its budget.
4. A Stop-hook block shows up in a transcript (from step 1).
5. A deps job opens its PR on the test repo, and its parent resumes after it
   merges.
6. Real decider calls on both Jev routes: a choice and a noul each.
7. `vigil doctor --repo thomasbardhi01/vigil`, against this vigild's API.
Budgets are the production caps from configs/budgets.yaml (2a round 2)."""

from __future__ import annotations

import json
import socket
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tests.conftest import auth, git
from tests.jobs.test_coord_loop import BarePublisher
from vigild.api.app import create_app
from vigild.config import RepoPolicy
from vigild.db import utcnow
from vigild.devimage import DevImages, DockerImages, SessionImages
from vigild.executors.runtime import DockerRuntime, DockerTestRunner
from vigild.gateway.app import create_gateway_app
from vigild.jobs.deps import DepsExecutor, HttpRegistry
from vigild.jobs.model import Role
from vigild.jobs.runner import JobRunner
from vigild.server import Vigild, build
from vigild.services import Tokens
from vigild.verifier import Verifier
from vigild.workspace import Workspace

pytestmark = [pytest.mark.docker, pytest.mark.live]

REPO = "o/donewhen"
EXCLUDE_NEWER = "2026-10-08T23:59:59Z"
PYPROJECT = (
    '[project]\nname = "donewhen"\nversion = "0.1.0"\nrequires-python = ">=3.14"\n'
    'dependencies = ["pytest==9.1.1"]\n'
)


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _seed(tmp: Path) -> dict[str, Path]:
    bare = tmp / "remote" / "donewhen.git"
    bare.parent.mkdir(parents=True)
    git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp / "seed"
    git(tmp, "init", "-q", "-b", "main", str(seed))
    git(seed, "config", "user.email", "t@vigil.invalid")
    git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("# donewhen\n\nThe phase 2a done-when's test repo.\n")
    (seed / "CLAUDE.md").write_text(
        "# Rules\n- Tests are pytest, under tests/. Run `pytest -q <file>`.\n"
        "- Never add a dependency: report needs_deps instead.\n"
    )
    (seed / "pyproject.toml").write_text(PYPROJECT)
    (seed / "docs").mkdir()
    (seed / "docs" / "notes.md").write_text("# Notes\n")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    subprocess.run(
        ["uv", "lock", "--exclude-newer", EXCLUDE_NEWER, "--python", "3.14.8"],
        cwd=seed, check=True, capture_output=True,
    )  # fmt: skip
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return {"bare": bare, "seed": seed}


def merge(tmp: Path, bare: Path, branch: str) -> str:
    """The owner merges a PR (on the test repo: a merge commit on main)."""
    work = tmp / f"merge-{branch.replace('/', '-')}-{time.time_ns()}"
    git(tmp, "clone", "-q", str(bare), str(work))
    git(work, "config", "user.email", "owner@vigil.invalid")
    git(work, "config", "user.name", "owner")
    git(work, "merge", "-q", "--no-ff", f"origin/{branch}", "-m", f"Merge {branch}")
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


@dataclass
class Live:
    v: Vigild
    runner: JobRunner
    publisher: BarePublisher
    client: TestClient
    tokens: Tokens
    bare: Path
    tmp: Path
    docker: Any


@pytest.fixture
def live(tmp_path: Path, tokens: Tokens) -> Iterator[Live]:
    for port in (8787, 8788):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                pytest.skip(f"port {port} is in use (stop vigild first)")
    import docker

    repo = _seed(tmp_path)
    v = build(data_dir=tmp_path / "vdata")
    if "anthropic" in v.services.degraded:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    cfg = v.cfg
    # workers on Haiku (the plan allows it); reviews Sonnet, audits Opus (review.yaml)
    for tier in cfg.routing.tiers:
        cfg.routing.tiers[tier] = "claude-haiku"
    cfg.routing.coord = cfg.routing.fallback = "claude-haiku"
    # the production caps and WIP limit (configs/budgets.yaml), as they are
    assert (cfg.budgets.per_task_usd, cfg.budgets.daily_usd, cfg.budgets.wip_per_repo) == (
        2.0, 5.0, 4)  # fmt: skip
    cfg.vigild.scheduler.checkpoint_seconds = 3
    cfg.repos.repos[REPO] = RepoPolicy(
        decider=["haiku", "jev"],
        backends=["claude-haiku", "claude-sonnet", "claude-opus"],
        dev_dockerfile="images/dev/fixture-uv.Dockerfile",
        dev_lockfiles=["pyproject.toml", "uv.lock"],
        dev_suites={"tests": "pytest -q -p no:cacheprovider"},
        dev_image_tools=["pytest", "python", "git", "grep", "uv"],
    )
    client = docker.from_env()
    ws = Workspace(tmp_path / "work", remote_url=lambda r: str(repo["bare"]))
    tests = DockerTestRunner(cfg, client)
    images = DevImages(cfg, v.store, DockerImages(client), ws, tests)
    mods = tmp_path / "claude-mods"

    def claude_mods() -> Path:
        if not mods.exists():
            src = Path.home() / "Documents" / "claude-mods"
            tar = tmp_path / "mods.tar"
            with tar.open("wb") as f:
                subprocess.run(
                    ["git", "-C", str(src), "archive", "--format=tar",
                     cfg.vigild.github.claude_mods_sha], stdout=f, check=True,
                )  # fmt: skip
            mods.mkdir()
            with tarfile.open(tar) as t:
                t.extractall(mods, filter="data")
        return mods

    publisher = BarePublisher(ws)
    runtime = DockerRuntime(cfg, client)

    def pr_state(r: str, url: str) -> str:
        n = int(url.rsplit("/", 1)[1])
        tip = git(repo["bare"], "rev-parse", publisher.prs[n - 1]["head"])
        p = subprocess.run(
            ["git", "merge-base", "--is-ancestor", tip, "main"], cwd=repo["bare"], check=False
        )
        return "merged" if p.returncode == 0 else "open"

    runner = JobRunner(
        cfg, v.store, v.budget, v.gateway, runtime, ws, publisher, v.telemetry, images,
        Verifier(ws, tests), session_images=SessionImages(images, claude_mods),
        background=True, breakers=v.gateway.breakers,
        deps=DepsExecutor(ws, runtime.run_deps, HttpRegistry(httpx2.Client(timeout=30)),
                          utcnow, tmp_path / "deps"),
        pr_state=pr_state,
    )  # fmt: skip
    images.on_failure = runner.dev_image_failed
    runner.session_images.on_failure = runner.dev_image_failed  # type: ignore[union-attr]
    v.services.jobs.runner = runner
    from vigild.services import Services

    # vigild's own API on its port, with its own token, for `vigil doctor` (step 7)
    api = Services(cfg=cfg, store=v.store, tokens=v.services.tokens, jobs=v.services.jobs)
    v.services.tokens = tokens
    runner.jobs = v.services.jobs
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        for app, port in ((create_gateway_app(v.gateway), 8788),
                          (create_app(api), cfg.vigild.api.port))
    ]  # fmt: skip
    threads = [threading.Thread(target=s.run, daemon=True) for s in servers]
    for t in threads:
        t.start()
    while not all(s.started for s in servers):
        time.sleep(0.1)
    with TestClient(create_app(v.services)) as c:
        yield Live(v, runner, publisher, c, tokens, repo["bare"], tmp_path, client)
    for s in servers:
        s.should_exit = True
    for t in threads:
        t.join(timeout=10)


def spend(lv: Live) -> float:
    return float(sum(r["cost_usd"] for r in lv.v.store.q("SELECT cost_usd FROM model_calls")))


def task(
    name: str, word: str, goal: str, files: list[str] | None = None, done: str | None = None
) -> dict[str, Any]:
    files = files or [f"docs/{name}.md", f"tests/test_{name}.py"]
    return {
        "repo": REPO,
        "why": f"the done-when needs {name}",
        "value": 3,
        "risk": 1,
        "labels": ["docs"],
        "size": "S",
        "spec": {
            "goal": goal,
            "test_cmd": f"pytest -q tests/test_{name}.py",
            "done_when": done or f"tests/test_{name}.py passes; docs/{name}.md says {word}",
            "files_in_scope": files,
            "must_fail_first": True,
        },
        "limits": {"max_turns": 30},  # the default cost cap; reservations are sized to the day
    }


def submit(lv: Live, body: dict[str, Any]) -> str:
    r = lv.client.post("/api/tasks", headers=auth(lv.tokens.admin), json=body)
    assert r.status_code == 202, r.text
    return str(r.json()["job_id"])


def inbox(lv: Live) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = lv.client.get(
        "/api/inbox", headers=auth(lv.tokens.read)
    ).json()
    return out


def approve(lv: Live, job: str) -> None:
    (item,) = [x for x in inbox(lv)["specs"] if x["job_id"] == job]
    r = lv.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(lv.tokens.admin), json={})
    assert r.status_code == 200, r.text


def wait(lv: Live, cond: Callable[[], bool], what: str, minutes: float = 25) -> None:
    end = time.monotonic() + minutes * 60
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(2)
    qs = [(q["title"], json.dumps(q["body"])[:1500]) for q in inbox(lv)["questions"]]
    raise AssertionError(f"timed out waiting for {what}; pages: {qs}")


def state(lv: Live, job: str) -> str:
    return lv.v.store.get_job(job).state.value


def pages(lv: Live) -> list[str]:
    return [f"{q['title']}: {json.dumps(q['body'])[:700]}" for q in inbox(lv)["questions"]]


def test_phase_2a_done_when(live: Live) -> None:
    lv = live
    store = lv.v.store
    t0 = spend(lv)

    # 1. three batch tasks, in parallel through the scheduler; reviewed and audited at rung 0
    hook_nudge = (
        " Work in two turns. In the first, write only the test, run it (it fails), and end "
        "your turn with a one-line status; don't write a report and don't use ask_coord. vigil "
        "will reply. Then write the doc line, run the test, write your report and commit."
    )
    jobs = [
        submit(lv, task("alpha", "alpha", "Add a line containing the word alpha to docs/alpha.md, "
                        "with a pytest test tests/test_alpha.py that reads the file and asserts it."
                        + hook_nudge)),
        submit(lv, task("bravo", "bravo", "Add a line containing the word bravo to docs/bravo.md, "
                        "with a pytest test tests/test_bravo.py that reads the file and asserts it.")),
        submit(lv, task("charlie", "charlie", "Add a line containing the word charlie to "
                        "docs/charlie.md, with a pytest test tests/test_charlie.py that reads the "
                        "file and asserts it.")),
        submit(lv, task("foxtrot", "foxtrot", "Add a line containing the word foxtrot to "
                        "docs/foxtrot.md, with a pytest test tests/test_foxtrot.py that reads the "
                        "file and asserts it.")),
    ]  # fmt: skip
    for j in jobs:
        approve(lv, j)
    wait(lv, lambda: all(state(lv, j) in ("ready", "blocked") for j in jobs), "four jobs")
    log("step 1:", {j: state(lv, j) for j in jobs}, f"spend ${spend(lv) - t0:.4f}", pages(lv))
    assert all(state(lv, j) == "ready" for j in jobs), pages(lv)
    log("session slots peak:", lv.runner.slots.peak)
    workers = [s for s in store.list_sessions() if s.role is Role.WORKER and s.job_id in jobs]
    spans = sorted((s.started_at or "", s.ended_at or "") for s in workers)
    overlap = sum(1 for a in spans for b in spans if a != b and a[0] < b[1] and b[0] < a[1]) // 2
    log(
        "workers:",
        [(s.task_id, s.started_at, s.ended_at, s.status) for s in workers],
        "overlaps",
        overlap,
    )
    assert lv.runner.slots.peak == 3 and overlap >= 1  # three at once: the fleet's limit
    sonnet = lv.v.cfg.preset("claude-sonnet").model
    opus = lv.v.cfg.preset("claude-opus").model
    by_role: dict[str, set[str]] = {"reviewer": set(), "auditor": set()}
    for s in store.list_sessions():
        if s.job_id in jobs and s.role.value in by_role:
            rows = store.q("SELECT model FROM model_calls WHERE session_id=?", (s.id,))
            by_role[s.role.value] |= {str(r["model"]) for r in rows}
    log("models by role:", by_role, "presets:", sonnet, opus)
    assert by_role == {"reviewer": {sonnet}, "auditor": {opus}}
    for j in jobs:
        rv = json.loads(store.get_artifact(j, "review.json", f"{j}-w1").content)  # type: ignore[union-attr]
        au = json.loads(store.get_artifact(j, "audit.json", f"{j}-w1").content)  # type: ignore[union-attr]
        log(f"{j}: review {rv['verdict']} rung {rv['rung']} by {rv['reviewer']}, "
            f"{len(rv['findings'])} findings; audit by {au['auditor']}, "
            f"{len(au['findings'])} findings, missed {sum(1 for f in au['findings'] if f.get('matched') is None)}")  # fmt: skip
        assert rv["rung"] == 0 and rv["audit_required"] and au["head"] == rv["head"]
    (ready,) = [x for x in inbox(lv)["ready"] if x["job_id"] == jobs[0]]
    log("ready to merge:", json.dumps(ready["body"])[:600])

    # 4. (from step 1) the Stop hook sent the alpha worker back, in its transcript
    alpha = [s for s in workers if s.job_id == jobs[0]]
    hook_events = [
        e.data for s in alpha for e in store.events(session_id=s.id) if e.type == "stop_hook"
    ]
    log("stop hook events:", json.dumps(hook_events)[:1500])
    blocked = [h for h in hook_events if h.get("blocked")]
    texts = [json.dumps(store.transcript(s.id)[1]) for s in alpha]
    shown = [t[t.find("vigil stop hook") :][:300] for t in texts if "vigil stop hook" in t]
    log("in the transcript:", shown[:1])
    assert blocked and shown
    t1 = spend(lv)

    # 2. the WIP breaker: four open PRs; a fifth task waits, one page, dispatch paused
    delta = task("delta", "delta",
                 "Create docs/d1.md, docs/d2.md and docs/d3.md, each with a line containing the "
                 "word delta, one file at a time, and run `pytest -q tests/test_delta.py` after "
                 "each one. The test tests/test_delta.py reads all three files and asserts the word.",
                 ["docs/d1.md", "docs/d2.md", "docs/d3.md", "tests/test_delta.py"],
                 "tests/test_delta.py passes; docs/d1.md, docs/d2.md and docs/d3.md say delta")  # fmt: skip
    d = submit(lv, delta)
    approve(lv, d)
    wait(lv, lambda: state(lv, d) == "blocked", "the WIP hold", 5)
    br = [q for q in inbox(lv)["questions"] if q["body"].get("kind") == "breaker"]
    log("step 2: breaker pages:", [(q["title"], q["body"]["detail"].get("stack")) for q in br])
    assert len(br) == 1 and br[0]["body"]["breaker_kind"] == "wip"
    assert not [s for s in store.list_sessions(d) if s.role is Role.WORKER]  # paused
    # the owner merges one PR; vigild's PR poll sees it; the owner resets the breaker
    merge(lv.tmp, lv.bare, f"vigil/{jobs[1]}-w1")
    log("merged:", lv.runner.poll_prs())
    killed: dict[str, Any] = {}

    def killer() -> None:
        """3. kill the delta worker once it has checkpointed."""
        end = time.monotonic() + 900
        while time.monotonic() < end:
            for s in store.list_sessions(d):
                if s.role is Role.WORKER and s.status == "running" and s.detail.get("checkpoint"):
                    killed["session"] = s.id
                    killed["checkpoint"] = s.detail["checkpoint"]
                    lv.docker.containers.get(f"vigil-{s.id}").kill()
                    log("killed", s.id, "at checkpoint", s.detail["checkpoint"][:12])
                    return
            time.sleep(0.3)

    k = threading.Thread(target=killer, daemon=True)
    k.start()
    r = lv.client.post(f"/api/inbox/{br[0]['id']}/reset", headers=auth(lv.tokens.admin))
    assert r.status_code == 200, r.text
    log("reset:", r.json())
    wait(lv, lambda: state(lv, d) in ("ready", "blocked"), "the delta job")
    k.join(timeout=5)
    log("step 3:", state(lv, d), killed, f"spend ${spend(lv) - t1:.4f}", pages(lv))
    assert state(lv, d) == "ready" and killed
    sessions = [s for s in store.list_sessions(d) if s.role is Role.WORKER]
    log("delta workers:", [(s.id, s.status, s.detail.get("checkpoint", "")[:12]) for s in sessions])
    crashed = store.get_session(killed["session"])
    assert crashed.status == "crashed"
    resumed = sessions[sessions.index(next(s for s in sessions if s.id == crashed.id)) + 1]
    forks = [e.data for e in store.events(session_id=resumed.id) if e.type == "fork"]
    log("resumed:", resumed.id, forks)
    assert (
        forks
        and forks[0]["from_session"] == crashed.id
        and forks[0]["start"] == killed["checkpoint"]
    )
    # within what was left of its budget: its reservation was the remainder, and
    # its metered spend stayed under it
    (held,) = store.q("SELECT amount_usd FROM reservations WHERE session_id=?", (resumed.id,))
    cap = float(store.get_task(f"{d}-w1").contract["limits"]["max_cost_usd"])
    before = sum(
        float(r["cost_usd"])
        for r in store.q("SELECT cost_usd FROM model_calls WHERE session_id=?", (crashed.id,))
    )
    after = sum(
        float(r["cost_usd"])
        for r in store.q("SELECT cost_usd FROM model_calls WHERE session_id=?", (resumed.id,))
    )
    log(f"resumed budget: cap ${cap:.2f}, spent before ${before:.4f}, "
        f"held ${held['amount_usd']:.4f}, spent ${after:.4f}")  # fmt: skip
    assert held["amount_usd"] <= cap - before + 1e-6 and after <= held["amount_usd"]
    t2 = spend(lv)

    # 5. a deps job: the echo worker needs six; the child job opens its PR; the parent resumes
    merge(lv.tmp, lv.bare, f"vigil/{jobs[2]}-w1")  # room under the WIP limit
    lv.runner.poll_prs()
    echo = task(
        "echo",
        "echo",
        "tests/test_echo.py must `import six` and assert `six.PY3`, and assert the word echo "
        "is in docs/echo.md; add that line to docs/echo.md. The package `six` is not "
        "installed and you must not add it: write your report with "
        '`"needs_deps": [{"ecosystem": "pypi", "package": "six", "version": "1.17.0", '
        '"why": "the test imports six"}]`, commit, and stop. '
        "When six is there, finish it.",
    )
    e = submit(lv, echo)
    approve(lv, e)
    wait(lv, lambda: state(lv, e) == "blocked", "the needs_deps block", 15)
    (nd,) = [
        q
        for q in inbox(lv)["questions"]
        if q["job_id"] == e and q["body"].get("kind") == "needs_deps"
    ]
    child = nd["body"]["deps_job"]
    log("step 5: deps job", child, "for", e, nd["body"]["tasks"][f"{e}-w1"]["needs_deps"])
    approve(lv, child)
    wait(lv, lambda: state(lv, child) in ("ready", "blocked"), "the deps job", 30)
    log("deps job:", state(lv, child), pages(lv))
    rep = store.get_artifact(child, "deps_report.json", f"{child}-w1")
    log("deps report:", rep.content[:1500] if rep else None)
    assert state(lv, child) == "ready"
    deps_pr = next(p for p in lv.publisher.prs if p["head"] == f"vigil/{child}-w1")
    files = git(lv.bare, "diff", "--name-only", "main", deps_pr["head"]).split()
    log("deps PR:", deps_pr["head"], files, deps_pr["body"][:400])
    assert files == ["pyproject.toml", "uv.lock"]
    merge(lv.tmp, lv.bare, deps_pr["head"])
    log("PR poll:", lv.runner.poll_prs())
    assert state(lv, child) == "merged"
    wait(lv, lambda: state(lv, e) in ("ready", "blocked"), "the parent after the merge", 30)
    log("parent:", state(lv, e), pages(lv))
    imgs = [s.detail.get("image") for s in store.list_sessions(e) if s.role is Role.WORKER]
    log("parent images:", imgs)
    assert state(lv, e) == "ready" and len(set(imgs)) == 2
    t3 = spend(lv)

    # 6. one real Jev call on each route, through the gateway's metered path:
    # the test repo is public (OpenRouter); a private fixture entry lists jev
    # too, and its live visibility sends it to Cloudflare
    from vigild.decider.questions import QUESTIONS
    from vigild.decider.wiring import build_decider
    from vigild.repos import set_visibility_source

    private_repo = "o/donewhen-private"
    lv.v.cfg.repos.repos[private_repo] = RepoPolicy(
        decider=["jev"], backends=[], dev_dockerfile="images/dev/fixture-uv.Dockerfile",
        dev_lockfiles=["uv.lock"], dev_suites={"t": "true"}, dev_image_tools=["pytest"],
    )  # fmt: skip
    set_visibility_source(lambda r: r == private_repo)
    decider = build_decider(lv.v.cfg, store, lv.v.gateway, lv.v.secrets, httpx2.Client(timeout=30))
    state_text = "Add three unit tests for the rate limiter in tests/test_rate_limit.py."
    decisions = {r: decider.decide(r, QUESTIONS["size"], state_text) for r in (REPO, private_repo)}
    # owner ruling 5: the noul criteria shape, live on both routes; P(true) is its probability
    nouls = {
        r: decider.decide(r, QUESTIONS["touches_money_path"], state_text)
        for r in (REPO, private_repo)
    }
    rows = [
        dict(r)
        for r in store.q(
            "SELECT question, kind, answer, probability, backend, model_version, action"
            " FROM decisions"
        )
    ]
    errs = [ev.data for ev in store.events() if ev.type == "decision"]
    jev = store.q(
        "SELECT provider, model, tokens_in, cost_usd FROM model_calls"
        " WHERE provider IN ('openrouter', 'cloudflare')"
    )
    log("step 6:", {r: (d.action.value, d.answer) for r, d in decisions.items()})
    log("step 6 noul:", {r: (d.action.value, d.answer) for r, d in nouls.items()})
    log("step 6 decisions rows:", rows)
    log("step 6 decider errors:", errs)
    log("step 6 metered Jev calls:", [dict(r) for r in jev])
    assert {r["provider"] for r in jev} == {"openrouter", "cloudflare"}
    noul_rows = {r["backend"] for r in rows if r["kind"] == "noul" and r["backend"] != "haiku"}
    assert noul_rows == {"jev-openrouter", "jev-cloudflare"}, (rows, errs)
    t4 = spend(lv)

    # 7. vigil doctor, as the owner runs it from a build session, against this
    # vigild's API: live calls only about thomasbardhi01/vigil (review A1). Its
    # dev-image lines read the data dir `vigil dev-image` built into
    # (VIGIL_DOCTOR_DATA_DIR), since this test's own has none.
    import os

    from vigild import config as config_mod
    from vigild.doctor import render, run_checks

    dcfg = config_mod.load()
    data = os.environ.get("VIGIL_DOCTOR_DATA_DIR")
    if data:
        dcfg.vigild.data_dir = data
    with httpx2.Client(timeout=30) as http:
        checks = run_checks(dcfg, lv.v.secrets, http=http, docker=lv.docker,
                            repos=["thomasbardhi01/vigil"])  # fmt: skip
    log("step 7: vigil doctor --repo thomasbardhi01/vigil\n" + render(checks))
    assert all(c.ok is not False for c in checks), render(checks)

    log(
        f"SPEND: step 1 ${t1 - t0:.4f}; steps 2-3 ${t2 - t1:.4f}; step 5 ${t3 - t2:.4f}; "
        f"step 6 ${t4 - t3:.4f}; total ${spend(lv) - t0:.4f}"
    )
