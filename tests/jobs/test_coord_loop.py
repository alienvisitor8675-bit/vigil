"""The coord loop end to end on a fixture repo (not ParkAgent):
request -> coord writes spec.md + plan.md -> spec gate -> inbox -> owner
approves -> planning -> two workers in parallel -> host checks + draft PRs
(against a local bare repo) -> worker_report.json -> job tree from one endpoint.

The executor here is a stand-in for the container that does what the harness
does (edit, report, commit); tests/jobs/test_coord_loop_docker.py runs the
real container."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth, git
from tests.harness import LocalTestRunner, finish_session
from tests.test_devimage import FakeDocker, FakeSuites
from vigild.api.app import create_app
from vigild.budget import Budget
from vigild.config import Config, RepoPolicy
from vigild.contract.models import CHECKLIST
from vigild.db import Store
from vigild.devimage import DevImages, SessionImages
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.jobs.model import Role
from vigild.jobs.runner import JobRunner
from vigild.jobs.service import JobService
from vigild.services import Services, Tokens
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier
from vigild.workspace import Workspace

KEY = b"k" * 32
REPO = "o/fixture"

SPEC = """# Spec: document the rate limit

## Goal
The feature map and the runbook both describe the rate limit.

## Non-goals
- Changing the limit.

## Acceptance criteria
- AC1: the feature map names the rate limit. test: `pytest -q tests/test_feature_map.py`
- AC2: the runbook says how to change it. test: `pytest -q tests/test_runbook.py`

## Files in scope
- `docs/feature-map.md`
- `docs/runbook.md`
- `tests/test_feature_map.py`
- `tests/test_runbook.py`

## Files out of scope
- `src/**`

## Money paths
none

## Interfaces
- None shared: each worker edits only its own doc and test (`docs/feature-map.md` and `tests/test_feature_map.py`, or `docs/runbook.md` and `tests/test_runbook.py`).

## Risks
- None.

## Rollback
Revert the PRs.
"""


def plan(job: str) -> str:
    workers = [
        {
            "task_id": f"{job}-w1",
            "goal": "feature map row",
            "why": "AC1",
            "value": 3,
            "risk": 1,
            "size": "XS",
            "test_cmd": "pytest -q tests/test_feature_map.py",
            "done_when": "AC1 holds",
            "files_in_scope": ["docs/feature-map.md", "tests/test_feature_map.py"],
            "labels": ["docs"],
        },
        {
            "task_id": f"{job}-w2",
            "goal": "runbook section",
            "why": "AC2",
            "value": 3,
            "risk": 1,
            "size": "XS",
            "test_cmd": "pytest -q tests/test_runbook.py",
            "done_when": "AC2 holds",
            "files_in_scope": ["docs/runbook.md", "tests/test_runbook.py"],
            "labels": ["docs"],
        },
    ]
    verify = [
        {"criterion": c, "level": 0, "why_not_higher": "a doc check has no property"}
        for c in ("AC1", "AC2")
    ]
    return (
        "# Plan\n\n## Notes addressed\n- keep it short: adopted\n\n## Workers\n```json\n"
        + json.dumps({"workers": workers, "verify": verify}, indent=1)
        + "\n```\n"
    )


EDITS = {
    "docs/feature-map.md": "# Feature map\n\n| rate limit | 10 req/min |\n",
    "docs/runbook.md": "# Runbook\n\nSet RATE_LIMIT in the env to change the limit.\n",
}
TESTS = {
    "docs/feature-map.md": (
        "tests/test_feature_map.py",
        "from pathlib import Path\n\n\ndef test_rate_limit():\n"
        "    assert 'rate limit' in Path('docs/feature-map.md').read_text()\n",
    ),
    "docs/runbook.md": (
        "tests/test_runbook.py",
        "from pathlib import Path\n\n\ndef test_rate_limit_var():\n"
        "    assert 'RATE_LIMIT' in Path('docs/runbook.md').read_text()\n",
    ),
}


CLEAN_REVIEW: dict[str, Any] = {
    "verdict": "clean",
    "findings": [],
    "checklist": {c: True for c in CHECKLIST},
}
CLEAN_AUDIT: dict[str, Any] = {"findings": [], "false_findings": []}


def transcript_lines(spec: SessionSpec) -> dict[str, Any]:
    """A session's transcript as the harness mirrors it: forked from the one it
    resumes (history first), then its own turn."""
    sdk = f"sdk-{spec.session_id}"
    history = list(spec.resume.entries) if spec.resume else []
    own = [
        {"type": "user", "uuid": f"u-{spec.session_id}", "message": {"role": "user",
         "content": spec.packet[:40]}},
        {"type": "assistant", "uuid": f"a-{spec.session_id}", "message": {"id": f"m-{spec.session_id}",
         "role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ]  # fmt: skip
    return {"session_id": sdk, "subpath": None, "entries": history + own}


def _work(clone: Path, doc: str, *, honest: bool = True) -> None:
    """A worker's change: its test first, then (if honest) the doc it checks."""
    test, text = TESTS[doc]
    (clone / test).parent.mkdir(parents=True, exist_ok=True)
    (clone / test).write_text(text)
    (clone / doc).write_text(EDITS[doc] if honest else "nothing relevant\n")


HIDDEN_BODY = (
    "from vigil_hidden import check, run_subject\n\n\n"
    "def test_flag_env_is_set():\n"
    "    r = run_subject(['python3', 'tools/check.py'])\n"
    "    check(r['returncode'] == 0 and r['stdout'].strip() == 'ok', repr(r))\n"
)
HIDDEN_NAME = "flag env is set by the runner"
Author = Callable[[SessionSpec, dict[str, Any]], "ExecOutcome | None"]


def good_author(body: str = HIDDEN_BODY) -> Author:
    """What a hidden-test author leaves in its clone: tests under the hidden
    directory and a manifest beside it."""

    def write(spec: SessionSpec, task: dict[str, Any]) -> ExecOutcome | None:
        folder = f".vigil/{spec.job_id}/hidden/{spec.task_id}"
        (spec.clone / folder).mkdir(parents=True, exist_ok=True)
        (spec.clone / folder / "test_hidden.py").write_text(body)
        manifest = {
            "test_cmd": f"pytest -q {folder}/test_hidden.py",
            "tests": [{"name": HIDDEN_NAME, "category": "happy-path"}],
        }
        (spec.clone / f"{folder}.json").write_text(json.dumps(manifest))
        return None

    return write


def failed_author(spec: SessionSpec, task: dict[str, Any]) -> ExecOutcome | None:
    return ExecOutcome("failed", 1, {}, "the author crashed")


def _fresh_ids(text: str, job: str, existing: list[str]) -> str:
    """What a coord does on a re-plan: a draft that names an id the job already has gets
    the next unused ones (the gate refuses a reused id; finding #4)."""
    if not existing or not any(f'"{e}"' in text for e in existing):
        return text
    head, rest = text.split("```json\n", 1)
    body, tail = rest.split("\n```", 1)
    doc = json.loads(body)
    top = max(int(e.rsplit("-w", 1)[1]) for e in existing)
    mapping = {w["task_id"]: f"{job}-w{top + i}" for i, w in enumerate(doc["workers"], 1)}
    for w in doc["workers"]:
        w["task_id"] = mapping[w["task_id"]]
        w["depends_on"] = [mapping.get(d, d) for d in w.get("depends_on", [])]
    return head + "```json\n" + json.dumps(doc) + "\n```" + tail


@dataclass
class StandIn:
    """Does what the container's harness does: run the agent's work, write the
    report, commit, leave the result bundle in the session's out dir, and put
    the head and the test record on the result line. Records concurrency."""

    coord_drafts: list[Callable[[str], tuple[str, str]]] = field(default_factory=list)
    coord_extra: Callable[[Path, str], None] | None = None
    worker_edit: Callable[[SessionSpec, dict[str, Any]], None] | None = None
    on_session: Callable[[SessionSpec], None] | None = None
    authors: list[Author] = field(default_factory=list)  # one per author session; default good
    packets: list[tuple[str, str]] = field(default_factory=list)  # (role, packet)
    # a reviewer's and an auditor's structured output (phase 2a, item 1)
    review: Callable[[SessionSpec], Any] = lambda spec: CLEAN_REVIEW
    audit: Callable[[SessionSpec], Any] = lambda spec: CLEAN_AUDIT
    specs: list[SessionSpec] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    running: int = 0
    max_running: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    barrier: threading.Barrier | None = None
    renumber: bool = True  # a re-plan's draft avoids the ids the job has (finding #4)

    def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome:
        assert gateway_token.startswith("vgw_")
        assert spec.packet.startswith("# 1. Role and rules")
        self.calls.append((spec.role.value, spec.task_id or spec.job_id))
        self.packets.append((spec.role.value, spec.packet))
        self.specs.append(spec)
        if self.on_session:
            self.on_session(spec)
        if spec.role in (Role.REVIEWER, Role.AUDITOR):
            answer = (self.review if spec.role is Role.REVIEWER else self.audit)(spec)
            return ExecOutcome("done", 0, {"status": "done", "structured_output": answer})
        # what the harness's session store mirrors: the transcript, as it's written
        on_event({"type": "transcript", "data": transcript_lines(spec)})
        task = json.loads((spec.clone / ".vigil" / "task.json").read_text())
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if spec.role is Role.HIDDEN_TEST_AUTHOR:
                behave = self.authors.pop(0) if self.authors else good_author()
                over = behave(spec, task)
                if over is not None:
                    return over
                git(spec.clone, "add", "-A")
                git(spec.clone, "commit", "-q", "--allow-empty", "-m", "hidden tests")
                result = finish_session(
                    spec.clone,
                    spec.session_dir / "out",
                    branch=task["branch"],
                    base_sha=task["base_sha"],
                    scope=[],
                )
                return ExecOutcome("done", 0, result)
            if spec.role is Role.COORD:
                draft = (
                    self.coord_drafts.pop(0) if self.coord_drafts else (lambda j: (SPEC, plan(j)))
                )
                sp, pl = draft(spec.job_id)
                if self.renumber:
                    pl = _fresh_ids(pl, spec.job_id, task.get("existing_task_ids") or [])
                d = spec.clone / ".vigil" / spec.job_id
                d.mkdir(parents=True, exist_ok=True)
                (d / "spec.md").write_text(sp)
                (d / "plan.md").write_text(pl)
                if self.coord_extra:
                    self.coord_extra(spec.clone, spec.job_id)
                git(spec.clone, "add", "-A")
                git(spec.clone, "commit", "-q", "--allow-empty", "-m", "coord: spec and plan")
                result = finish_session(
                    spec.clone,
                    spec.session_dir / "out",
                    branch=task["branch"],
                    base_sha=task["base_sha"],
                    scope=[],
                )
                return ExecOutcome("done", 0, result)
            if self.barrier:
                self.barrier.wait(timeout=10)  # both workers must be in flight at once
            if self.worker_edit:
                self.worker_edit(spec, task)
            else:
                f = task["spec"]["files_in_scope"][0]
                _work(spec.clone, f)
            d = spec.clone / ".vigil" / spec.job_id / spec.task_id  # type: ignore[operator]
            d.mkdir(parents=True, exist_ok=True)
            if not (d / "ask_coord.json").exists() and not (d / "worker_report.json").exists():
                (d / "worker_report.json").write_text(json.dumps({
                    "task_id": spec.task_id, "job_id": spec.job_id, "test_before": "fail",
                    "test_after": "pass", "files_changed": task["spec"]["files_in_scope"],
                    "summary": "documented it",
                    "self_check": {"test_failed_first": True, "outside_scope": [],
                                   "not_done": "nothing", "reviewer_would_flag": "wording",
                                   "could_delete": "nothing"},
                    "open_questions": [], "provenance": "claude"}))  # fmt: skip
            result = finish_session(
                spec.clone,
                spec.session_dir / "out",
                branch=task["branch"],
                base_sha=task["base_sha"],
                scope=list(task["spec"]["files_in_scope"]),
            )
            return ExecOutcome("done", 0, result)
        finally:
            with self.lock:
                self.running -= 1


@dataclass
class BarePublisher:
    """GitHub stand-in: vigild's real publish, pushing to the local bare repo."""

    ws: Workspace | None = None
    prs: list[dict[str, Any]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def close(self, repo: str, url: str, job_id: str | None = None) -> None:
        self.closed.append(url)

    def publish(
        self,
        *,
        repo: str,
        head: str,
        base_sha: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        scope: list[str],
        existing: str | None = None,
        job_id: str | None = None,
    ) -> str:
        from vigild.github.publish import publish

        prs = self.prs

        class Pulls:
            def create_pull(
                self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
            ) -> str:
                prs.append(
                    {"head": head, "base": base, "title": title, "draft": draft, "body": body}
                )
                return f"https://github.com/{repo}/pull/{len(prs)}"

        assert self.ws is not None
        return publish(
            self.ws,
            repo=repo,
            head=head,
            base_sha=base_sha,
            branch=branch,
            base=base,
            title=title,
            body=body,
            pulls=Pulls(),
            scope=scope,
            existing=existing,
        )


@dataclass
class World:
    client: TestClient
    store: Store
    budget: Budget
    standin: StandIn
    publisher: BarePublisher
    bare: Path
    tokens: Tokens
    images: DevImages
    runner: JobRunner
    tests: LocalTestRunner


@pytest.fixture
def world(
    cfg: Config, tmp_path: Path, fixture_repo: dict[str, Path], tokens: Tokens
) -> Iterator[World]:
    cfg.repos.repos[REPO] = RepoPolicy(
        decider=["haiku"],
        backends=["claude-sonnet", "claude-opus", "claude-haiku"],
        dev_dockerfile="images/dev/vigil.Dockerfile",
        dev_lockfiles=["uv.lock"],
        dev_suites={"smoke": "true"},
        dev_image_tools=["git", "grep", "pytest"],
    )
    store = Store.open(":memory:")
    budget = Budget(store, cfg.budgets)
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway

    gateway = Gateway(cfg, store, budget, Secrets({}))
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    images = DevImages(cfg, store, FakeDocker(), ws, FakeSuites())
    standin, publisher, tests = StandIn(), BarePublisher(ws), LocalTestRunner()
    runner = JobRunner(
        cfg,
        store,
        budget,
        gateway,
        standin,
        ws,
        publisher,
        Telemetry(),
        images,
        Verifier(ws, tests, host_key=KEY),
        background=False,
        session_images=SessionImages(images, lambda: tmp_path),
    )
    images.on_failure = runner.dev_image_failed
    assert runner.session_images is not None
    runner.session_images.on_failure = runner.dev_image_failed
    jobs = JobService(cfg, store, runner)
    runner.jobs = jobs
    services = Services(cfg=cfg, store=store, tokens=tokens, jobs=jobs)
    with TestClient(create_app(services)) as c:
        yield World(
            c,
            store,
            budget,
            standin,
            publisher,
            fixture_repo["bare"],
            tokens,
            images,
            runner,
            tests,
        )


def _request(w: World) -> str:
    r = w.client.post(
        "/api/jobs",
        headers=auth(w.tokens.admin),
        json={
            "repo": REPO,
            "title": "Document the rate limit",
            "request": "The limit is undocumented.",
            "notes": ["keep it short"],
            "why": "support asks weekly",
            "value": 3,
            "risk": 1,
        },
    )
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


def test_end_to_end(world: World) -> None:
    w = world
    w.standin.barrier = threading.Barrier(2)
    job = _request(w)

    # the coord drafted; the spec gate passed; the owner sees it in the inbox
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()
    (item,) = inbox["specs"]
    assert item["job_id"] == job
    assert [x["task_id"] for x in item["body"]["workers"]] == [f"{job}-w1", f"{job}-w2"]
    # the owner sees the exact command the verifier will run (round 2, N2)
    assert [x["test_cmd"] for x in item["body"]["workers"]] == [
        "pytest -q tests/test_feature_map.py",
        "pytest -q tests/test_runbook.py",
    ]
    assert all(x["must_fail_first"] for x in item["body"]["workers"])
    assert w.store.get_job(job).state.value == "spec_pending"
    assert [c[0] for c in w.standin.calls] == ["coord"]

    # approve from the inbox: plan, then two workers in parallel
    r = w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert r.status_code == 200, r.text
    assert w.standin.max_running == 2
    assert w.store.get_job(job).state.value == "ready"

    # two draft PRs, from the host; `.vigil/` never reaches the target repo (Tier 1)
    # and the vault still holds each worker's whole result
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w1", f"vigil/{job}-w2"]
    assert all(p["draft"] for p in w.publisher.prs)
    for n in (1, 2):
        files = git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{job}-w{n}")
        assert ".vigil" not in files and files
        vault_head = w.runner._task_head(f"{job}-w{n}")
        assert vault_head
        kept = w.runner.workspace.tree_paths(REPO, vault_head)
        assert f".vigil/{job}/{job}-w{n}/worker_report.json" in kept
        assert f".vigil/{job}/spec.md" in kept and f".vigil/{job}/plan.md" in kept
        # every pr_open record says so, from the vault's tree of what was sent
        ev = json.loads(
            w.store.q(
                "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC",
                (f"{job}-w{n}",),
            )[0]["data"]
        )
        assert ev["vigil_in_pr"] is False and ev["published"] and ev["head"] == vault_head
    assert git(w.bare, "log", "-1", "--format=%s", "main") == "seed"

    # the whole tree from one endpoint
    tree = w.client.get(f"/api/jobs/{job}/tree", headers=auth(w.tokens.read)).json()
    assert tree["job"]["state"] == "ready"
    (coord,) = tree["sessions"]
    assert coord["role"] == "coord" and coord["status"] == "done"
    workers = coord["children"]
    assert sorted(x["role"] for x in workers) == ["worker", "worker"]
    assert all(x["status"] == "pr_open" and x["pr_url"] for x in workers)
    names = sorted(a["name"] for a in tree["artifacts"])
    assert names.count("worker_report.json") == 2
    assert {"spec.md", "plan.md", "approval.json", "spec_gate.json"} <= set(names)
    assert {t["state"] for t in tree["tasks"]} == {"reviewed"}
    # every reservation was settled
    assert w.budget.open_today() == 0
    # stages 5 and 6 (phase 2a): each PR reviewed in a read-only session, then
    # audited (rung 0 audits all of them); the job waits in "Ready to merge"
    roles = [c[0] for c in w.standin.calls]
    assert roles.count("reviewer") == 2 and roles.count("auditor") == 2
    assert names.count("review.json") == 2 and names.count("audit.json") == 2
    reviewers = [s for s in w.standin.specs if s.role is Role.REVIEWER]
    assert all(s.output_schema and "checklist" in s.output_schema["properties"] for s in reviewers)
    assert all(s.preset == "claude-sonnet" for s in reviewers)
    auditors = [s for s in w.standin.specs if s.role is Role.AUDITOR]
    assert all(s.preset == "claude-opus" for s in auditors)
    (ready,) = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["ready"]
    assert ready["job_id"] == job and len(ready["body"]["prs"]) == 2
    assert all(p["audited"] and p["rung"] == 0 for p in ready["body"]["prs"])


def test_coord_revises_after_a_gate_failure(world: World) -> None:
    w = world
    bad = SPEC.replace(" test: `pytest -q tests/test_runbook.py`", "")
    w.standin.coord_drafts = [lambda j: (bad, plan(j))]
    job = _request(w)
    assert [c[0] for c in w.standin.calls] == ["coord", "coord"]
    (item,) = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    assert item["job_id"] == job


def test_coord_pages_the_owner_after_three_failed_drafts(world: World) -> None:
    w = world
    bad = SPEC.replace(" test: `pytest -q tests/test_runbook.py`", "")
    w.standin.coord_drafts = [lambda j: (bad, plan(j))] * 3
    job = _request(w)
    inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()
    assert inbox["specs"] == []
    assert any(q["job_id"] == job and "spec gate" in q["title"] for q in inbox["questions"])
    assert w.store.get_job(job).state.value == "spec_pending"


def _approve(w: World) -> str:
    job = _request(w)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    return job


def _events(w: World, kind: str) -> list[dict[str, Any]]:
    rows = w.store.q("SELECT data, task_id FROM events WHERE type=? ORDER BY id", (kind,))
    return [{**json.loads(r["data"]), "task_id": r["task_id"]} for r in rows]


def _no_failure_pages(w: World) -> bool:
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    return not any("cap reached" in p["title"] or "failed" in p["title"] for p in qs)


def test_a_day_that_is_full_queues_the_coord_then_runs_it(world: World) -> None:
    """A reservation that doesn't fit the day's headroom waits; it neither
    fails nor pages nor uses a draft. It runs when money frees up."""
    w = world
    held = [
        w.budget.reserve(session_id=None, task_id=f"other-{i}", amount=a, purpose="worker")
        for i, a in enumerate((2.0, 2.0, 1.0))
    ]
    polls: list[float] = []

    def sleep(s: float) -> None:
        polls.append(s)
        for rid in held:  # another job finishes: the day has room
            w.budget.settle(rid, 0.0)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    job = _request(w)
    assert polls, "the coord should have waited"
    assert [e["task_id"] for e in _events(w, "queued")] == [None]
    assert len(_events(w, "dequeued")) == 1
    assert _no_failure_pages(w)
    assert w.store.get_job(job).state.value == "spec_pending"  # coord ran: spec awaits approval
    assert [c[0] for c in w.standin.calls] == ["coord"]  # one draft: no attempt used


def test_a_reservation_over_the_daily_cap_itself_is_refused_not_queued(world: World) -> None:
    w = world
    w.runner.cfg.budgets.daily_usd = 1.0  # a $2 coord can never fit this day
    w.runner.sleep = lambda s: pytest.fail("must not wait for money that can't exist")  # type: ignore[method-assign]
    _request(w)
    assert w.standin.calls == []
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert any(p["body"].get("cap") == "daily" for p in pages)
    assert _events(w, "queued") == []


def test_a_dev_image_that_fails_refuses_dispatch_and_offers_rebuild(world: World) -> None:
    w = world
    w.images.docker.ok = False  # type: ignore[attr-defined]  # the build fails
    job = _request(w)
    assert w.standin.calls == []
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert any(p["job_id"] == job and "dev image" in p["title"] for p in pages)
    (item,) = [p for p in pages if p["body"].get("kind") == "dev_image"]
    assert item["body"]["actions"] == ["rebuild", "dismiss"] and item["body"]["stage"] == "build"

    w.images.docker.ok = True  # type: ignore[attr-defined]  # fixed; the owner rebuilds
    r = w.client.post(f"/api/inbox/{item['id']}/rebuild", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.images.status(REPO, item["body"]["base_sha"])[0]
    job_page = next(p for p in pages if p["job_id"] == job)
    r = w.client.post(f"/api/inbox/{job_page['id']}/resume", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert [c[0] for c in w.standin.calls] == ["coord"]


def test_ask_coord_files_a_question(world: World) -> None:
    w = world

    def ask(spec: SessionSpec, task: dict[str, Any]) -> None:
        d = spec.clone / ".vigil" / spec.job_id / str(spec.task_id)
        d.mkdir(parents=True, exist_ok=True)
        if spec.task_id and spec.task_id.endswith("w2"):
            (d / "ask_coord.json").write_text(json.dumps({"question": "Which env var name?"}))
        else:
            f = task["spec"]["files_in_scope"][0]
            _work(spec.clone, f)

    w.standin.worker_edit = ask
    job = _approve(w)
    questions = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    q = next(q for q in questions if q["kind"] == "ask_coord")
    assert q["job_id"] == job and q["body"]["question"] == "Which env var name?"
    assert q["body"]["coord_session_id"] == w.store.get_job(job).coord_session_id
    assert [p["head"] for p in w.publisher.prs] == [f"vigil/{job}-w1"]
    assert w.store.get_task(f"{job}-w2").state == "waiting"


def test_out_of_scope_worker_is_not_published(world: World) -> None:
    w = world

    def sneaky(spec: SessionSpec, task: dict[str, Any]) -> None:
        f = task["spec"]["files_in_scope"][0]
        _work(spec.clone, f)
        (spec.clone / "README.md").write_text("sneaky\n")

    w.standin.worker_edit = sneaky
    job = _approve(w)
    assert w.publisher.prs == []
    assert {t.state for t in w.store.list_tasks(job)} == {"failed"}
    assert w.store.get_job(job).state.value == "blocked"  # round 2, N5: never stranded
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert any("acceptance" in p["title"] for p in pages)


def test_a_session_without_guard_fails_closed(world: World) -> None:
    w = world

    class NoGuard(StandIn):
        def run(self, spec: SessionSpec, gateway_token: str, on_event: EventSink) -> ExecOutcome:
            if spec.role is Role.COORD:
                return super().run(spec, gateway_token, on_event)
            return ExecOutcome(
                "failed", 3, {"status": "failed", "error": "guard not loaded"}, "guard not loaded"
            )

    w.runner.executor = NoGuard()
    job = _approve(w)
    assert w.publisher.prs == []
    assert {t.state for t in w.store.list_tasks(job)} == {"failed"}


# --- review round 1 -----------------------------------------------------------------------


def test_the_reports_test_claims_are_ignored(world: World) -> None:  # round 2, N1
    """A worker whose report says pass, but whose change doesn't pass test_cmd
    in the verifier, gets no PR."""
    w = world

    def lie(spec: SessionSpec, task: dict[str, Any]) -> None:
        f = task["spec"]["files_in_scope"][0]
        _work(spec.clone, f, honest=False)

    w.standin.worker_edit = lie
    job = _approve(w)
    assert w.publisher.prs == []
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert any("verifier saw it fail after" in json.dumps(p["body"]) for p in pages)
    assert {t.state for t in w.store.list_tasks(job)} == {"failed"}
    record = w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1")
    assert record is not None and record.provenance == "human"


def test_hook_forgery_and_a_fifo_record_change_no_verdict(
    world: World, tmp_path: Path
) -> None:  # round 2, N1 + N3, the reviewer's replay
    """Both workers plant commondir -> a copy with core.fsmonitor and
    hooks/post-commit that write a passing record, and a FIFO at the old record
    path. w1 does the work, w2 doesn't: w1 gets its PR, w2 doesn't, nothing
    planted runs, and the FIFO never blocks vigild."""
    w = world
    marker = tmp_path / "PWNED"

    def hostile(spec: SessionSpec, task: dict[str, Any]) -> None:
        out = spec.session_dir / "out"
        evil = tmp_path / f"evil-{spec.task_id}"
        (evil / "hooks").mkdir(parents=True)
        forge = (
            f"#!/bin/sh\necho ran >> {marker}\n"
            f'echo \'{{"before":{{"outcome":"fail"}},"after":{{"outcome":"pass"}}}}\''
            f" > {out}/harness_tests.json\n"
        )
        for name in ("post-commit", "pre-commit"):
            (evil / "hooks" / name).write_text(forge)
            (evil / "hooks" / name).chmod(0o755)
        (evil / "fsmonitor").write_text(forge)
        (evil / "fsmonitor").chmod(0o755)
        (evil / "config").write_text(
            f"[core]\n\thooksPath = {evil / 'hooks'}\n\tfsmonitor = {evil / 'fsmonitor'}\n"
        )
        for d in ("objects", "refs"):
            os.symlink(spec.clone / ".git" / d, evil / d)
        (spec.clone / ".git" / "commondir").write_text(str(evil) + "\n")
        out.mkdir(parents=True, exist_ok=True)
        os.mkfifo(out / "harness_tests.json")
        f = task["spec"]["files_in_scope"][0]
        honest = bool(spec.task_id and spec.task_id.endswith("w1"))
        _work(spec.clone, f, honest=honest)

    w.standin.worker_edit = hostile
    done = threading.Event()
    job_box: list[str] = []

    def go() -> None:
        job_box.append(_approve(w))
        done.set()

    threading.Thread(target=go, daemon=True).start()
    assert done.wait(60), "vigild blocked"
    job = job_box[0]
    assert [p["head"] for p in w.publisher.prs] == [f"vigil/{job}-w1"]
    assert w.store.get_task(f"{job}-w1").state == "pr_open"
    assert w.store.get_task(f"{job}-w2").state == "failed"
    assert not marker.exists()
    for call in w.tests.calls:
        assert call["hooksPath"] == "/dev/null" and not call["worker_files"]


def test_a_coord_symlink_never_reads_a_host_file(world: World, tmp_path: Path) -> None:  # blocker 3
    w = world
    secret = tmp_path / "pretend-env"
    secret.write_text("ANTHROPIC_API_KEY=must-not-leak")

    def plant(clone: Path, job_id: str) -> None:
        spec = clone / ".vigil" / job_id / "spec.md"
        spec.unlink()
        os.symlink(secret, spec)

    w.standin.coord_extra = plant
    job = _request(w)
    for a in w.store.q("SELECT content FROM artifacts WHERE job_id=?", (job,)):
        assert "must-not-leak" not in a["content"]


def test_each_coord_draft_is_its_own_session_and_books_once(world: World) -> None:  # item 8
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    w = world
    bad = SPEC.replace(" test: `pytest -q tests/test_runbook.py`", "")
    w.standin.coord_drafts = [lambda j: (bad, plan(j))]
    prices = PriceTable(w.runner.cfg.prices)

    def spend(spec: SessionSpec) -> None:  # $0.10 of Opus output per draft
        record_call(
            w.store,
            prices,
            session_id=spec.session_id,
            task_id=None,
            job_id=spec.job_id,
            purpose="session",
            provider="anthropic",
            backend_preset="claude-opus",
            model="claude-opus-5-5",
            usage=CallUsage(tokens_out=5000),
        )

    w.standin.on_session = spend
    job = _request(w)
    coords = [s for s in w.store.list_sessions(job) if s.role is Role.COORD]
    assert len(coords) == 2 and coords[0].id != coords[1].id
    assert w.store.get_job(job).coord_session_id == coords[1].id
    booked = w.store.q("SELECT SUM(amount_usd) a FROM ledger")[0]["a"]
    metered = w.store.q("SELECT SUM(cost_usd) c FROM model_calls")[0]["c"]
    assert booked == pytest.approx(metered) == pytest.approx(0.20)


def test_dependents_of_a_failed_worker_wait_and_the_job_blocks(world: World) -> None:  # item 17
    w = world

    def chained(j: str) -> tuple[str, str]:
        p = plan(j).replace(
            '"files_in_scope": [\n    "docs/runbook.md",\n    "tests/test_runbook.py"\n   ],',
            '"files_in_scope": [\n    "docs/runbook.md",\n    "tests/test_runbook.py"\n   ],\n   "depends_on": ["'
            + j
            + '-w1"],',
        )
        assert "depends_on" in p
        return SPEC, p

    def fail_w1(spec: SessionSpec, task: dict[str, Any]) -> None:
        f = task["spec"]["files_in_scope"][0]
        _work(spec.clone, f)
        if spec.task_id and spec.task_id.endswith("w1"):
            (spec.clone / "README.md").write_text("out of scope\n")

    w.standin.coord_drafts = [chained]
    w.standin.worker_edit = fail_w1
    job = _approve(w)
    assert [c[1] for c in w.standin.calls if c[0] == "worker"] == [f"{job}-w1"]
    assert w.store.get_job(job).state.value == "blocked"
    item = _failure_item(w, job)
    assert list(item["body"]["tasks"]) == [f"{job}-w1"] and item["body"]["resumable"]
    assert w.store.get_task(f"{job}-w2").state == "queued"


def test_a_full_day_queues_the_workers_and_they_run_when_it_frees(world: World) -> None:  # item 18
    w = world
    job = _request(w)
    held = [
        w.budget.reserve(session_id=None, task_id=f"other-{i}", amount=a, purpose="worker")
        for i, a in enumerate((2.0, 2.0, 0.98))
    ]

    def sleep(s: float) -> None:
        for rid in held:  # the other work finished
            w.budget.settle(rid, 0.0)

    w.runner.sleep = sleep  # type: ignore[method-assign]
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(job).state.value == "ready"
    assert len(w.publisher.prs) == 2
    assert len(_events(w, "queued")) == 2 and len(_events(w, "dequeued")) == 2
    assert _no_failure_pages(w)


def test_a_failure_before_the_session_never_leaks_the_reservation(  # item 7
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    job = _request(w)
    real = w.runner.workspace.clone

    def broken(repo: str, task_id: str, **kw: Any) -> Path:
        if "-w" in task_id:
            raise OSError("disk full")
        return real(repo, task_id, **kw)

    monkeypatch.setattr(w.runner.workspace, "clone", broken)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.budget.open_today() == 0
    assert {t.state for t in w.store.list_tasks(job)} == {"failed"}


def test_reconcile_after_a_crash(world: World) -> None:  # item 7
    from vigild.jobs.fsm import Actor, transition
    from vigild.jobs.model import JobState

    w = world
    job = _request(w)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    w.runner.background = True  # pretend the build is mid-flight when vigild dies
    w.runner.build = lambda j: None  # type: ignore[method-assign]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    transition(w.store, job, JobState.PLANNING, Actor.VIGILD)
    s = w.store.create_session(job, Role.WORKER, backend_preset="claude-sonnet")
    w.store.update_session(s.id, status="running")
    w.budget.reserve(session_id=s.id, task_id=f"{job}-w1", amount=1.0, purpose="worker")
    tok = w.runner.gateway.issue_token(s.id, REPO, "claude-sonnet")

    out = w.runner.reconcile()  # what vigild's startup runs
    assert out["reservations"] == 1 and out["held"] == 1 and out["tokens"] >= 1
    assert w.budget.open_today() == 0
    assert w.store.get_session(s.id).status == "failed"
    assert w.store.get_job(job).state.value == "blocked"
    pages = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert any("restarted" in p["title"] and p["body"]["resumable"] for p in pages)
    from vigild.gateway.service import GatewayError

    with pytest.raises(GatewayError):
        w.runner.gateway.authorize(tok)


def test_reconcile_restarts_an_approved_job_that_never_built(world: World) -> None:  # item 19
    w = world
    job = _request(w)
    item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    real_build = w.runner.build
    w.runner.build = lambda j: None  # type: ignore[method-assign]  # vigild dies after the commit
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(job).state.value == "approved"
    w.runner.build = real_build  # type: ignore[method-assign]
    assert w.runner.reconcile()["rebuilt"] == 1
    # nothing starts until vigild's gateway is up and its images are validated (N11)
    assert w.store.get_job(job).state.value == "approved" and w.standin.calls == [("coord", job)]
    assert w.runner.start_deferred() == 1
    assert w.store.get_job(job).state.value == "ready"
    assert w.runner.start_deferred() == 0


# --- review round 2, N5: a failed worker blocks its job; retry, re-plan or abandon ----------


def _spend(w: World, amount_out_tokens: int) -> Callable[[SessionSpec], None]:
    """Each worker session spends Opus output tokens through the meter."""
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    prices = PriceTable(w.runner.cfg.prices)

    def spend(spec: SessionSpec) -> None:
        if spec.role is Role.WORKER:
            record_call(
                w.store,
                prices,
                session_id=spec.session_id,
                task_id=spec.task_id,
                job_id=spec.job_id,
                purpose="session",
                provider="anthropic",
                backend_preset="claude-opus",
                model="claude-opus-5-5",
                usage=CallUsage(tokens_out=amount_out_tokens),
            )

    return spend


def _failure_item(w: World, job: str | None, kind: str = "worker_failed") -> dict[str, Any]:
    items = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (item,) = [q for q in items if q["job_id"] == job and q["body"].get("kind") == kind]
    return item  # type: ignore[no-any-return]


def _flaky(fail_first: set[str]) -> Callable[[SessionSpec, dict[str, Any]], None]:
    """Workers whose first attempt strays out of scope; later attempts are clean."""
    tries: dict[str, int] = {}

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        tid = str(spec.task_id)
        tries[tid] = tries.get(tid, 0) + 1
        f = task["spec"]["files_in_scope"][0]
        _work(spec.clone, f)
        if tries[tid] == 1 and tid[-2:] in fail_first:
            (spec.clone / "README.md").write_text("out of scope\n")

    return edit


def test_fail_blocked_retry_done(world: World) -> None:
    """End to end: a worker fails, the job blocks with one inbox item offering
    retry, re-plan or abandon; retry re-runs only the failed task, with what's
    left of its budget; the job reaches review."""
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    w.standin.on_session = _spend(w, 15_000)  # $0.30 of Opus output per worker session
    budgets: list[tuple[str, float]] = []
    real = w.standin.run

    def run(spec: SessionSpec, token: str, sink: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER:
            budgets.append((str(spec.task_id), spec.max_budget_usd))
        return real(spec, token, sink)

    w.standin.run = run  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    item = _failure_item(w, job)
    assert item["body"]["actions"] == ["retry", "replan", "abandon"]
    failed = item["body"]["tasks"]
    assert list(failed) == [f"{job}-w1"]
    assert "outside scope" in json.dumps(failed)
    assert failed[f"{job}-w1"]["spent_usd"] == pytest.approx(0.30)
    assert failed[f"{job}-w1"]["remaining_usd"] == pytest.approx(1.70)
    assert [p["head"] for p in w.publisher.prs] == [f"vigil/{job}-w2"]

    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "ready"
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w1", f"vigil/{job}-w2"]
    # the retry ran only w1, under what was left of its $2.00, not a fresh cap
    w1 = [b for b in budgets if b[0].endswith("w1")]
    assert w1 == [(f"{job}-w1", 2.0), (f"{job}-w1", pytest.approx(1.70))]
    assert sum(1 for b in budgets if b[0].endswith("w2")) == 1
    held = w.store.q(
        "SELECT amount_usd FROM reservations WHERE task_id=? ORDER BY id", (f"{job}-w1",)
    )
    assert [r["amount_usd"] for r in held] == [2.0, pytest.approx(1.70)]
    assert w.budget.open_today() == 0


def test_no_budget_left_offers_only_replan_or_abandon(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    w.standin.on_session = _spend(w, 100_000)  # $2.00: the whole cap
    job = _approve(w)
    item = _failure_item(w, job)
    assert item["body"]["actions"] == ["replan", "abandon"]
    assert item["body"]["resumable"] is False
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 409
    r = w.client.post(f"/api/inbox/{item['id']}/resume", headers=auth(w.tokens.admin))
    assert r.status_code == 409


def test_replan_sends_the_job_back_to_the_coord_with_the_failure(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    packets: list[str] = []
    w.standin.on_session = lambda spec: (
        packets.append(spec.packet) if spec.role is Role.COORD else None
    )
    job = _approve(w)
    item = _failure_item(w, job)
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert len(packets) == 2
    assert f"{job}-w1" in packets[1] and "outside scope" in packets[1]
    assert "outside scope" not in packets[0]
    # the new draft goes through approval again, then builds; w2's PR stands
    assert w.store.get_job(job).state.value == "spec_pending"
    spec_item = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]
    r = w.client.post(
        f"/api/inbox/{spec_item['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    assert r.status_code == 200, r.text
    qs = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    assert w.store.get_job(job).state.value == "ready", [(q["title"], q["body"]) for q in qs]
    # the re-plan's tasks are new ones (w3, w4); w1 is superseded and w2's PR stands
    assert sorted(p["head"] for p in w.publisher.prs) == [f"vigil/{job}-w{n}" for n in (2, 3, 4)]
    assert w.store.get_task(f"{job}-w1").state == "superseded"


def test_abandon_ends_the_job(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    item = _failure_item(w, job)
    calls = len(w.standin.calls)
    r = w.client.post(f"/api/inbox/{item['id']}/abandon", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "abandoned"
    assert w.store.get_task(f"{job}-w1").state == "abandoned"
    assert w.store.get_task(f"{job}-w2").state == "pr_open"
    assert len(w.standin.calls) == calls
    assert w.budget.open_today() == 0
    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code in (404, 409)


@pytest.mark.parametrize("cause", ["ask_coord", "stale_image", "session_failed"])
def test_every_worker_failure_blocks_with_actions(world: World, cause: str) -> None:
    w = world

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        f = task["spec"]["files_in_scope"][0]
        _work(spec.clone, f)
        if cause == "ask_coord" and str(spec.task_id).endswith("w1"):
            d = spec.clone / ".vigil" / spec.job_id / str(spec.task_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "ask_coord.json").write_text(json.dumps({"question": "which?"}))

    w.standin.worker_edit = edit
    if cause == "stale_image":
        real_fresh = w.images.ensure_fresh
        n = [0]

        def fresh(repo: str, base_sha: str, force: bool = False) -> str:
            n[0] += 1
            if n[0] > 1:  # the coord ran; the image is gone and won't build again
                w.images.docker.ids.clear()  # type: ignore[attr-defined]
                w.images.docker.ok = False  # type: ignore[attr-defined]
            return real_fresh(repo, base_sha, force=force)

        w.images.ensure_fresh = fresh  # type: ignore[method-assign]
    if cause == "session_failed":
        real = w.standin.run

        def run(spec: SessionSpec, token: str, sink: EventSink) -> ExecOutcome:
            if spec.role is Role.WORKER and str(spec.task_id).endswith("w1"):
                return ExecOutcome("timeout", None, {}, "over 40 min")
            return real(spec, token, sink)

        w.standin.run = run  # type: ignore[method-assign]
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    item = _failure_item(w, job)
    assert "replan" in item["body"]["actions"] and "abandon" in item["body"]["actions"]
    assert f"{job}-w1" in item["body"]["tasks"]
    assert w.budget.open_today() == 0


def test_reconcile_offers_retry_or_cancel_for_a_coord_it_failed(world: World) -> None:  # N6
    w = world
    w.runner.run_coord = lambda j: None  # type: ignore[method-assign]  # vigild dies drafting
    job = _request(w)
    coord = w.store.create_session(job, Role.COORD, backend_preset="claude-opus")
    w.store.set_coord_session(job, coord.id)
    w.store.update_session(coord.id, status="running")
    del w.runner.run_coord  # back to the real one

    out = w.runner.reconcile()
    assert out["sessions"] == 1 and out["held"] == 1
    assert w.store.get_session(coord.id).status == "failed"
    items = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
    (item,) = [q for q in items if q["job_id"] == job]
    assert "restarted" in item["title"]
    assert item["body"]["actions"] == ["retry", "abandon"] and item["body"]["resumable"]

    r = w.client.post(f"/api/inbox/{item['id']}/retry", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert [c[0] for c in w.standin.calls] == ["coord"]
    assert w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]


def test_cancel_a_spec_pending_job(world: World) -> None:  # N6
    w = world
    w.runner.run_coord = lambda j: None  # type: ignore[method-assign]
    job = _request(w)
    coord = w.store.create_session(job, Role.COORD, backend_preset="claude-opus")
    w.store.update_session(coord.id, status="running")
    w.runner.reconcile()
    item = next(
        q
        for q in w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["questions"]
        if q["job_id"] == job
    )
    r = w.client.post(f"/api/inbox/{item['id']}/abandon", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert w.store.get_job(job).state.value == "abandoned"


# --- review round 3, F1: runner config ------------------------------------------------------


def test_an_unnamed_runner_config_change_is_rejected(world: World) -> None:
    w = world
    w.runner.cfg.repos.repos[REPO].runner_config = ["conftest.py"]

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        (spec.clone / "conftest.py").write_text("collect_ignore = ['tests']\n")
        # committed by the agent itself (an untracked file out of scope would
        # simply be left out of the commit, round 4)
        git(spec.clone, "add", "conftest.py")
        git(spec.clone, "commit", "-qm", "narrow the tests")

    w.standin.worker_edit = edit
    job = _approve(w)
    assert w.publisher.prs == []
    item = _failure_item(w, job)
    assert "changes how tests run" in json.dumps(item["body"])


def _named(j: str) -> tuple[str, str]:
    """A spec whose first worker names tools/check.py, a runner file."""
    sp = SPEC.replace(
        "- `tests/test_runbook.py`\n", "- `tests/test_runbook.py`\n- `tools/check.py`\n"
    )
    pl = plan(j).replace(
        '"tests/test_feature_map.py"\n   ]',
        '"tests/test_feature_map.py",\n    "tools/check.py"\n   ]',
    )
    assert "tools/check.py" in pl
    return sp, pl


TOOL_OK = 'print("ok")\n'


def _runner_world(w: World) -> None:
    w.runner.cfg.repos.repos[REPO].runner_config = ["tools/check.py"]
    w.runner.cfg.repos.repos[REPO].hidden_tests.enabled = True  # the switch is off by default
    w.standin.coord_drafts = [_named]


def _flag_edit(tool: str = TOOL_OK) -> Callable[[SessionSpec, dict[str, Any]], None]:
    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if str(spec.task_id).endswith("w1"):
            (spec.clone / "tools").mkdir(exist_ok=True)
            (spec.clone / "tools/check.py").write_text(tool)

    return edit


def _spec_item(w: World) -> dict[str, Any]:
    return w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"][0]  # type: ignore[no-any-return]


def test_the_author_runs_before_the_spec_is_filed_and_the_inbox_shows_names_only(
    world: World,
) -> None:
    w = world
    _runner_world(w)
    seen: list[tuple[str, int, str]] = []

    def watch(spec: SessionSpec) -> None:
        if spec.role is Role.HIDDEN_TEST_AUTHOR:
            inbox = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
            seen.append((w.store.get_job(spec.job_id).state.value, len(inbox), spec.packet))

    w.standin.on_session = watch
    job = _request(w)
    # while the author ran: spec_pending, and nothing to approve yet
    assert [(st, n) for st, n, _ in seen] == [("spec_pending", 0)]
    assert [c[0] for c in w.standin.calls] == ["coord", "hidden_test_author"]
    item = _spec_item(w)
    by = {x["task_id"]: x for x in item["body"]["workers"]}
    w1, w2 = by[f"{job}-w1"], by[f"{job}-w2"]
    assert w1["runner_file_change"] == ["tools/check.py"] and w2["runner_file_change"] == []
    assert w1["hidden_tests"] == {
        "status": "ok",
        "tests": [{"name": HIDDEN_NAME, "category": "happy-path"}],
        "attempts": 1,
    }
    assert "hidden_tests" not in w2
    assert "test_hidden.py" not in json.dumps(item) and "run_subject" not in json.dumps(item)
    # the author is a fresh, role-recorded session under its own cap
    (author,) = [x for x in w.store.list_sessions(job) if x.role is Role.HIDDEN_TEST_AUTHOR]
    assert author.parent_session_id is None and author.detail["for_task"] == f"{job}-w1"
    res = w.store.q("SELECT purpose, amount_usd FROM reservations WHERE session_id=?", (author.id,))
    assert [(r["purpose"], r["amount_usd"]) for r in res] == [("hidden_tests", 1.0)]
    assert w.budget.open_today() == 0
    # the packet holds the spec and this task's contract, not the plan
    packet = seen[0][2]
    assert "## spec.md (approved)" in packet and f"{job}-w1" in packet
    assert f"{job}-w2" not in packet and "keep it short" not in packet
    # artifacts: names for the owner; the code only in the vault
    art = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)
    assert art["provenance"] == "claude" and art["tasks"][f"{job}-w1"]["status"] == "ok"
    gate = json.loads(w.store.get_artifact(job, "spec_gate.json").content)
    assert gate["runner_file_change"] == {f"{job}-w1": ["tools/check.py"]}


def test_a_runner_file_task_passes_on_its_hidden_tests_and_the_pr_says_so(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.worker_edit = _flag_edit()
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    pr = next(p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w1")
    assert "Owner review required: runner file changed: `tools/check.py`" in pr["body"]
    assert pr["draft"]
    other = next(p for p in w.publisher.prs if p["head"] == f"vigil/{job}-w2")
    assert "Owner review required" not in other["body"]
    # the marker is durable: on the task's contract and on the pr_open event
    assert w.store.get_task(f"{job}-w1").contract["runner_file_change"] == ["tools/check.py"]
    assert w.store.get_task(f"{job}-w2").contract["runner_file_change"] == []
    (opened,) = [e for e in _events(w, "pr_open") if e["task_id"] == f"{job}-w1"]
    assert opened["runner_file_change"] == ["tools/check.py"]
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert rec["runner_files"] == ["tools/check.py"] and rec["runner_file_changed"] is True
    assert rec["runner_config"]["pinned_to_base"] == []
    assert rec["after"]["outcome"] == "pass"  # the head's tool is the subject, run as a subprocess
    assert rec["hidden"]["ok"] and rec["hidden"]["base"]["outcome"] == "fail"
    assert rec["hidden"]["head"]["outcome"] == "pass"


def test_hidden_tests_never_reach_a_worker_or_the_branch(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.worker_edit = _flag_edit()
    clones: dict[str, bool] = {}

    def look(spec: SessionSpec) -> None:
        if spec.role is Role.WORKER:
            hidden = spec.clone / ".vigil" / spec.job_id / "hidden"
            sha = json.loads(w.store.get_artifact(spec.job_id, "hidden_tests.json").content)[
                "tasks"
            ][f"{spec.job_id}-w1"]["sha"]
            has = subprocess_has_object(spec.clone, sha)
            clones[str(spec.task_id)] = (
                hidden.exists()
                or has
                or any(m for m in spec.mounts if "/.vigil/" in str(m) and "/hidden" in str(m))
            )

    w.standin.on_session = look
    job = _approve(w)
    assert clones == {f"{job}-w1": False, f"{job}-w2": False}
    for role, packet in w.standin.packets:
        if role == "worker":
            assert HIDDEN_NAME not in packet and "run_subject" not in packet
            assert "test_hidden.py" not in packet and "/hidden/" not in packet
    for n in (1, 2):
        files = git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{job}-w{n}")
        assert "hidden" not in files
    assert "hidden" not in git(w.bare, "branch", "-a")


def subprocess_has_object(repo: Path, sha: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


def test_hidden_tests_decide_even_when_the_visible_test_passes(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.worker_edit = _flag_edit('print("bad")\n')  # visible test passes; the tool says bad
    job = _approve(w)
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]
    item = _failure_item(w, job)
    body = json.dumps(item["body"])
    assert "hidden tests must pass on the head" in body
    assert HIDDEN_NAME not in body and "test_hidden" not in body and "run_subject" not in body
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert rec["after"]["outcome"] == "pass" and rec["hidden"]["ok"] is False


def test_an_author_that_fails_twice_leaves_the_task_able_only_to_escalate(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.authors = [failed_author, failed_author]
    w.standin.worker_edit = _flag_edit()
    job = _request(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 2
    item = _spec_item(w)
    hidden = {x["task_id"]: x for x in item["body"]["workers"]}[f"{job}-w1"]["hidden_tests"]
    assert hidden["status"] == "missing"
    assert hidden["note"] == "hidden tests missing; this task can only escalate"
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    # built and verified, but never passed: no PR, an escalation with an inbox item
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]
    assert w.store.get_task(f"{job}-w1").state == "escalated"
    assert w.store.get_job(job).state.value == "escalated"
    page = _failure_item(w, job, kind="escalated")
    assert "no hidden tests" in json.dumps(page["body"])
    assert page["body"]["actions"] == ["replan", "abandon"]
    assert [e["task"] for e in _events(w, "escalated")] == [f"{job}-w1"]
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert rec["after"]["outcome"] == "pass" and rec["hidden"]["present"] is False


def test_a_test_that_errors_instead_of_failing_an_assertion_is_regenerated_once(
    world: World,
) -> None:
    w = world
    _runner_world(w)
    w.standin.authors = [good_author("import nothing_here_exists\n\n\ndef test_x():\n    pass\n")]
    job = _request(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 2
    authors = [p for r, p in w.standin.packets if r == "hidden_test_author"]
    assert "errored" in authors[1] and "errored" not in authors[0]
    hidden = {x["task_id"]: x for x in _spec_item(w)["body"]["workers"]}[f"{job}-w1"]
    assert hidden["hidden_tests"]["status"] == "ok" and hidden["hidden_tests"]["attempts"] == 2


def test_tests_that_fail_the_wrong_way_twice_escalate_the_task(world: World) -> None:
    w = world
    _runner_world(w)
    bad = good_author("import nothing_here_exists\n\n\ndef test_x():\n    pass\n")
    w.standin.authors = [bad, bad]
    w.standin.worker_edit = _flag_edit()
    job = _approve(w)
    assert [c[0] for c in w.standin.calls].count("hidden_test_author") == 2
    assert w.store.get_task(f"{job}-w1").state == "escalated"
    assert f"vigil/{job}-w1" not in [p["head"] for p in w.publisher.prs]


def test_tests_that_pass_on_the_base_are_rejected(world: World) -> None:
    w = world
    _runner_world(w)
    easy = good_author("def test_x():\n    assert True\n")
    w.standin.authors = [easy, easy]
    job = _request(w)
    hidden = {x["task_id"]: x for x in _spec_item(w)["body"]["workers"]}[f"{job}-w1"]
    assert hidden["hidden_tests"]["status"] == "missing"
    assert "pass on the base" in hidden["hidden_tests"]["reason"]


def test_an_author_that_edits_the_repo_is_rejected(world: World) -> None:
    w = world
    _runner_world(w)

    def meddler(spec: SessionSpec, task: dict[str, Any]) -> ExecOutcome | None:
        good_author()(spec, task)
        (spec.clone / "docs" / "runbook.md").write_text("changed\n")
        return None

    w.standin.authors = [meddler, meddler]
    job = _request(w)
    hidden = {x["task_id"]: x for x in _spec_item(w)["body"]["workers"]}[f"{job}-w1"]
    assert hidden["hidden_tests"]["status"] == "missing"
    assert "outside the hidden directory" in hidden["hidden_tests"]["reason"]


def test_a_repo_that_may_not_use_the_reviewer_tier_gets_no_author(world: World) -> None:
    w = world
    _runner_world(w)
    w.runner.cfg.repos.repos[REPO].backends = ["claude-opus"]
    job = _request(w)
    assert "hidden_test_author" not in [c[0] for c in w.standin.calls]
    hidden = {x["task_id"]: x for x in _spec_item(w)["body"]["workers"]}[f"{job}-w1"]
    assert hidden["hidden_tests"]["status"] == "missing"
    assert "backend refused" in hidden["hidden_tests"]["reason"]


def test_an_authors_session_is_never_a_workers_or_a_reviewers(world: World) -> None:
    from vigild.db import SessionRoleError

    w = world
    _runner_world(w)
    job = _request(w)
    (author,) = [x for x in w.store.list_sessions(job) if x.role is Role.HIDDEN_TEST_AUTHOR]
    for role in (Role.WORKER, Role.REVIEWER):
        with pytest.raises(SessionRoleError):  # a continuation of the author's context
            w.store.create_session(job, role, parent_session_id=author.id)
    with pytest.raises(SessionRoleError):  # and the author is always fresh
        w.store.create_session(
            job, Role.HIDDEN_TEST_AUTHOR, parent_session_id=w.store.get_job(job).coord_session_id
        )
    # at dispatch: the author's row can't run as a worker
    spec = SessionSpec(
        session_id=author.id, role=Role.WORKER, job_id=job, task_id=None, repo=REPO,
        preset="claude-sonnet", model="m", clone=Path("."), session_dir=Path("."),
        max_turns=1, max_minutes=1, max_budget_usd=0.1,
    )  # fmt: skip
    with pytest.raises(SessionRoleError):
        w.runner._run_session(author, spec, 0, {})


def test_hidden_show_prints_the_tests_and_records_that_a_human_saw_them(world: World) -> None:
    from vigild.hidden import HiddenError, show

    w = world
    _runner_world(w)
    job = _request(w)
    _approve_item(w)
    out = show(w.store, w.runner.workspace, f"{job}-w1")
    assert HIDDEN_NAME in out and "run_subject" in out and "test_hidden.py" in out
    (ev,) = _events(w, "hidden_tests_shown")
    assert ev["task"] == f"{job}-w1" and ev["task_id"] == f"{job}-w1"
    row = w.store.q("SELECT provenance FROM events WHERE type='hidden_tests_shown'")[0]
    assert row["provenance"] == "human"
    with pytest.raises(HiddenError):
        show(w.store, w.runner.workspace, f"{job}-w2")  # no hidden tests for an ordinary task


def _approve_item(w: World) -> None:
    item = _spec_item(w)
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})


def test_no_route_serves_hidden_tests(world: World) -> None:
    paths = {getattr(r, "path", "") for r in world.client.app.routes}  # type: ignore[attr-defined]
    assert not [p for p in paths if "hidden" in p]


def test_an_api_submit_of_a_runner_file_task_starts_no_session_and_files_no_item(
    client: TestClient, services: Any, runner: Any, tokens: Tokens
) -> None:
    """The author never blocks a submit: the runner does it afterwards."""
    services.cfg.repos.repos["thomasbardhi01/vigil"].runner_config = ["conftest.py"]
    services.cfg.repos.repos["thomasbardhi01/vigil"].hidden_tests.enabled = True
    r = client.post(
        "/api/tasks",
        headers=auth(tokens.admin),
        json={
            "repo": "thomasbardhi01/vigil", "why": "w", "value": 3, "risk": 1,
            "runner_file_change": ["not.this"],
            "spec": {"goal": "g", "test_cmd": "pytest -q tests/test_a.py", "done_when": "d",
                     "files_in_scope": ["conftest.py", "tests/test_a.py"]},
        },
    )  # fmt: skip
    assert r.status_code in (200, 202), r.text
    job = r.json()["job_id"]
    assert client.get("/api/inbox", headers=auth(tokens.read)).json()["specs"] == []
    assert runner.created == [job]
    assert services.jobs.flagged_tasks(job) == {f"{job}-w1": ["conftest.py"]}
    # vigild computes the class; a caller's value is replaced
    assert services.store.get_task(f"{job}-w1").contract["runner_file_change"] == ["conftest.py"]


# --- owner direction, round 3: a task that needs a dependency blocks on it --------------------


def test_a_worker_that_reports_needs_deps_blocks_its_job(world: World) -> None:
    w = world

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if str(spec.task_id).endswith("w1"):
            d = spec.clone / ".vigil" / spec.job_id / str(spec.task_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "worker_report.json").write_text(json.dumps({
                "task_id": spec.task_id, "job_id": spec.job_id, "files_changed": [],
                "summary": "needs a parser", "provenance": "claude",
                "self_check": {"test_failed_first": True, "outside_scope": [], "not_done": "all",
                               "reviewer_would_flag": "-", "could_delete": "-"},
                "needs_deps": [{"ecosystem": "pypi", "package": "tomlkit", "version": "0.13.2",
                                "why": "round-trip TOML"}]}))  # fmt: skip

    w.standin.worker_edit = edit
    job = _approve(w)
    assert w.store.get_job(job).state.value == "blocked"
    assert w.store.get_task(f"{job}-w1").state == "needs_deps"
    item = _failure_item(w, job, kind="needs_deps")
    deps = item["body"]["tasks"][f"{job}-w1"]["needs_deps"]
    assert deps == [{"ecosystem": "pypi", "package": "tomlkit", "version": "0.13.2",
                     "why": "round-trip TOML"}]  # fmt: skip
    # phase 2a, item 5: a child deps job, waiting for approval; the job resumes on its own
    assert item["body"]["actions"] == ["abandon"]
    child = item["body"]["deps_job"]
    assert w.store.get_job(job).blocked_on == child
    assert w.store.get_job(child).state.value == "spec_pending"
    assert w.tests.calls == [c for c in w.tests.calls if f"{job}-w1" not in c["source"]]


def test_a_lockfile_change_is_read_as_needs_deps(world: World) -> None:
    """The worker edits the manifest instead of asking: vigild reads what it
    wanted from the diff, shows the diff fenced, and doesn't verify the task."""
    w = world
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["package.json"]
    w.runner.cfg.repos.repos[REPO].runner_config = ["package.json"]

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if str(spec.task_id).endswith("w1"):
            (spec.clone / "package.json").write_text('{"dependencies": {"left-pad": "1.3.0"}}\n')
            git(spec.clone, "add", "package.json")  # the agent commits it itself
            git(spec.clone, "commit", "-qm", "add left-pad")

    w.standin.worker_edit = edit
    job = _approve(w)
    item = _failure_item(w, job, kind="needs_deps")
    t = item["body"]["tasks"][f"{job}-w1"]
    assert t["needs_deps"] == [
        {"ecosystem": "npm", "package": "left-pad", "version": "1.3.0", "why": "read from the diff"}
    ]
    assert t["diff"].startswith("<<<UNTRUSTED") and '+{"dependencies"' in t["diff"]


def test_needs_deps_is_inferred_from_manifests() -> None:
    from vigild.jobs.deps import infer_deps

    pkg_a = '{"dependencies": {"a": "1.0.0"}, "devDependencies": {"b": "2.0.0"}}'
    pkg_b = '{"dependencies": {"a": "1.1.0", "c": "^3"}, "devDependencies": {"b": "2.0.0"}}'
    got = infer_deps("server/package.json", pkg_a, pkg_b)
    assert [(d.ecosystem, d.package, d.version) for d in got] == [
        ("npm", "a", "1.1.0"),
        ("npm", "c", "^3"),
    ]
    py_a = '[project]\nname = "x"\ndependencies = ["requests>=2.32"]\n'
    py_b = '[project]\nname = "x"\ndependencies = ["requests>=2.32", "tomlkit==0.13.2"]\n'
    got = infer_deps("data/pyproject.toml", py_a, py_b)
    assert [(d.ecosystem, d.package, d.version) for d in got] == [("pypi", "tomlkit", "==0.13.2")]
    assert infer_deps("uv.lock", "a", "b") == []


def test_a_job_already_running_is_not_started_twice(world: World) -> None:  # round 3, F7
    w = world
    job = _request(w)
    started: list[str] = []
    gate = threading.Event()

    def slow(j: str) -> None:
        started.append(j)
        gate.wait(5)

    w.runner.background = True
    w.runner.run_coord = slow  # type: ignore[method-assign]
    assert w.runner.on_resume(job) == "started"
    # a second resume while the first runs is queued, never run at once (2a review B4)
    assert w.runner.on_resume(job) == "queued"
    assert w.runner.on_resume(job) == "queued"  # and asked twice, it runs once more
    time.sleep(0.05)
    assert started == [job]
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and w.runner.running():
        time.sleep(0.01)
    assert started == [job, job]


def test_sessions_run_in_the_session_image_and_tests_in_the_dev_image(world: World) -> None:
    w = world  # owner direction, round 3
    seen: list[tuple[str, str | None]] = []
    w.standin.on_session = lambda spec: seen.append((spec.role.value, spec.image))
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    assert {r for r, _ in seen} == {"coord", "worker", "reviewer", "auditor"}
    assert all(img and img.startswith("vigil-session/fixture:") for _, img in seen), seen
    images = {c["image"] for c in w.tests.calls}
    assert images and all(i.startswith("vigil-dev/fixture:") for i in images), images


def test_build_output_a_worker_leaves_is_not_committed(world: World) -> None:  # round 4, item 7
    """A worker that ran pytest in its session leaves __pycache__ and build
    output: they aren't committed, so the scope check still passes."""
    w = world

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        (spec.clone / "tests" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (spec.clone / "tests" / "__pycache__" / "t.cpython-314.pyc").write_bytes(b"\0")
        (spec.clone / ".pytest_cache").mkdir(exist_ok=True)
        (spec.clone / ".pytest_cache" / "README.md").write_text("cache\n")

    w.standin.worker_edit = edit
    job = _approve(w)
    assert w.store.get_job(job).state.value == "ready"
    assert len(w.publisher.prs) == 2


def test_a_scripts_edit_is_runner_config_not_needs_deps(world: World) -> None:  # round 4, item 8
    w = world
    w.runner.cfg.repos.repos[REPO].dev_lockfiles = ["package.json"]
    w.runner.cfg.repos.repos[REPO].runner_config = ["package.json"]

    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        _work(spec.clone, task["spec"]["files_in_scope"][0])
        if str(spec.task_id).endswith("w1"):
            (spec.clone / "package.json").write_text('{"scripts": {"test": "vitest run x"}}\n')
            git(spec.clone, "add", "package.json")
            git(spec.clone, "commit", "-qm", "narrow the test script")

    w.standin.worker_edit = edit
    job = _approve(w)
    assert w.store.get_task(f"{job}-w1").state == "failed"  # not needs_deps
    item = _failure_item(w, job)
    assert "changes how tests run" in json.dumps(item["body"])


def test_coord_and_worker_sessions_cap_each_calls_output(world: World) -> None:
    """The gateway holds max_tokens of output against the reservation, so every
    coord and worker session carries the cap from configs/budgets.yaml."""
    w = world
    seen: list[tuple[Role, int | None]] = []
    w.standin.on_session = lambda spec: seen.append((spec.role, spec.max_output_tokens))
    _approve(w)
    cap = 16384  # session_max_output_tokens in configs/budgets.yaml
    roles = {r for r, _ in seen}
    assert {Role.COORD, Role.WORKER} <= roles
    assert all(n == cap for r, n in seen if r in (Role.COORD, Role.WORKER))
    # a reviewer or auditor has its own, smaller cap (2a: REVIEW_OUTPUT_TOKENS, which the
    # $0.40 and $0.80 session caps were measured at)
    assert all(n == 8192 for r, n in seen if r in (Role.REVIEWER, Role.AUDITOR))


def test_a_mid_run_budget_refusal_names_the_cap_and_the_shortfall_on_the_page(
    world: World,
) -> None:
    """On main a refused call returns 403 to the CLI and the session ends; it
    does not pause (checkpoint resume is 2a). The page it raises must say why."""
    w = world
    real = w.standin.run

    def run(spec: SessionSpec, token: str, sink: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER:
            w.store.add_event(
                "gateway_refused",
                {
                    "status": 403,
                    "reason": "budget: this session's output cap is 16384 tokens per call; the "
                    "session has $0.0100 of its reservation left, enough for 500 output tokens",
                },
                "human",
                job_id=spec.job_id,
                session_id=spec.session_id,
                task_id=spec.task_id,
            )
            return ExecOutcome("failed", 1, error="API Error: 403")
        return real(spec, token, sink)

    w.standin.run = run  # type: ignore[method-assign]
    job = _approve(w)
    item = _failure_item(w, job)
    err = json.dumps(item["body"])
    assert "output cap is 16384" in err and "$0.0100" in err


def test_a_session_whose_last_turn_hit_the_cap_is_not_done(world: World) -> None:
    """stop_reason max_tokens on the final model call: the session failed, and
    its page says the turn was truncated at the cap."""
    w = world

    def truncate(spec: SessionSpec) -> None:
        if spec.role is Role.COORD:
            from vigild.metering.calls import CallUsage, record_call
            from vigild.metering.prices import PriceTable

            record_call(
                w.store,
                PriceTable(w.runner.cfg.prices),
                session_id=spec.session_id,
                task_id=None,
                job_id=spec.job_id,
                purpose="session",
                provider="anthropic",
                backend_preset="claude-opus",
                model="claude-opus-5-5",
                usage=CallUsage(tokens_out=16384),
                truncated=True,
            )

    w.standin.on_session = truncate
    _request(w)
    pages = json.dumps(w.client.get("/api/inbox", headers=auth(w.tokens.read)).json())
    assert "truncated at the output cap" in pages


def test_a_job_whose_first_call_cannot_fit_is_refused_before_any_session(world: World) -> None:
    """Preflight at submission: a 422 with the numbers, no job, no session, no attempt."""
    w = world
    w.runner.cfg.budgets.coord_session_usd = 0.50
    before = (len(w.store.list_jobs()), w.store.q("SELECT COUNT(*) n FROM sessions")[0]["n"])
    r = w.client.post(
        "/api/jobs",
        headers=auth(w.tokens.admin),
        json={"repo": REPO, "title": "t", "request": "r", "why": "w", "value": 3, "risk": 1},
    )
    assert r.status_code == 422
    assert "$0.50" in json.dumps(r.json()) and "claude-opus-5-5" in json.dumps(r.json())
    after = (len(w.store.list_jobs()), w.store.q("SELECT COUNT(*) n FROM sessions")[0]["n"])
    assert after == before
    assert w.standin.calls == []  # no container, so no attempt
    assert w.store.q("SELECT COUNT(*) n FROM reservations")[0]["n"] == 0
    assert w.store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 0
    # the same refusal for a task
    r = w.client.post("/api/tasks", headers=auth(w.tokens.admin), json={"repo": REPO})
    assert r.status_code == 422


def _work_for(spec: SessionSpec, task: dict[str, Any]) -> None:
    """A worker's honest change to the one doc its task owns."""
    f = task["spec"]["files_in_scope"][0]
    if f in EDITS:
        _work(spec.clone, f)
        return
    (spec.clone / "tests").mkdir(exist_ok=True)
    (spec.clone / "tests" / "test_extra.py").write_text(
        "from pathlib import Path\n\n\ndef test_extra():\n"
        "    assert 'extra' in Path('docs/extra.md').read_text()\n"
    )
    (spec.clone / "docs").mkdir(exist_ok=True)
    (spec.clone / "docs" / "extra.md").write_text("extra\n")


def _peak_open(w: World) -> list[float]:
    """Open reservations (today) seen as each session starts."""
    seen: list[float] = []
    w.standin.on_session = lambda spec: seen.append(w.budget.open_today())
    return seen


def test_one_jobs_peak_reservation_fits_the_daily_cap(world: World) -> None:
    """A job's peak is max(coord reservation, the first wave's workers, the
    review stage's reserve): the coord settles before workers start and the
    workers before the review stage is reserved (2a, from the phase 1 run).
    Two independent workers reserve per_task_usd each; the peak must fit under
    the daily cap, and the reviewers and auditors draw from one stage reserve."""
    w = world
    seen = _peak_open(w)
    in_review: list[float] = []
    inner = w.standin.on_session

    def on_session(spec: SessionSpec) -> None:
        if inner is not None:
            inner(spec)
        if spec.role in (Role.REVIEWER, Role.AUDITOR):
            in_review.append(w.budget.open_today())

    w.standin.on_session = on_session
    job = _approve(w)
    cfg = w.runner.cfg.budgets
    wave = len([t for t in w.store.list_tasks(job) if not t.contract.get("depends_on")])
    assert wave == 2
    stage = (cfg.review_session_usd + cfg.audit_session_usd) * wave
    assert max(seen) == pytest.approx(max(cfg.coord_session_usd, wave * cfg.per_task_usd, stage))
    assert max(seen) <= cfg.daily_usd
    assert in_review and max(in_review) == pytest.approx(stage)  # the first draws from all of it
    assert w.budget.open_today() == 0


def test_a_third_worker_waits_for_the_day_then_runs(world: World) -> None:
    """Three workers at $2 against a $5 day: two run, the third queues, and it
    runs once one of them settles. Nothing fails, nothing pages, no attempt is
    used, and the open reservations never pass the daily cap."""
    w = world
    cfg = w.runner.cfg.budgets
    assert 3 * cfg.per_task_usd > cfg.daily_usd  # the premise, from the real config

    def three(job: str) -> tuple[str, str]:
        doc = json.loads(plan(job).split("```json\n")[1].split("\n```")[0])
        w3 = {
            **doc["workers"][1],
            "task_id": f"{job}-w3",
            "goal": "third",
            "test_cmd": "pytest -q tests/test_extra.py",
            "files_in_scope": ["docs/extra.md", "tests/test_extra.py"],
        }
        doc["workers"].append(w3)
        text = plan(job).split("```json\n")[0] + "```json\n" + json.dumps(doc) + "\n```\n"
        spec = SPEC.replace(
            "- `tests/test_runbook.py`\n",
            "- `tests/test_runbook.py`\n- `docs/extra.md`\n- `tests/test_extra.py`\n",
        )
        return spec, text

    w.standin.coord_drafts = [three]
    w.runner.sleep = lambda s: time.sleep(0.01)  # type: ignore[method-assign]
    seen = _peak_open(w)
    w.standin.worker_edit = _work_for
    job = _approve(w)
    workers = [c for c in w.standin.calls if c[0] == "worker"]
    assert len(workers) == 3
    assert w.standin.max_running <= 2
    assert max(seen) <= cfg.daily_usd
    queued = _events(w, "queued")
    assert [e["task_id"] for e in queued] == [f"{job}-w3"]
    assert [e["task_id"] for e in _events(w, "dequeued")] == [f"{job}-w3"]
    assert _no_failure_pages(w)
    assert w.store.get_job(job).state.value == "ready"
    assert w.budget.open_today() == 0


def test_a_failed_end_of_session_settle_fails_closed(world: World) -> None:
    """If the runner's settle raises, the session is not done, its reservation is
    booked at the amount reserved (not released at $0), and the owner is paged."""
    w = world
    real = w.budget.settle

    def settle(rid: int, amount: float | None = None) -> float:
        if amount is None:
            raise RuntimeError("ledger write failed")
        return real(rid, amount)

    w.budget.settle = settle  # type: ignore[method-assign]
    _request(w)
    pages = json.dumps(w.client.get("/api/inbox", headers=auth(w.tokens.read)).json())
    assert "metering failed: " in pages and "ledger write failed" in pages
    assert w.store.q("SELECT 1 FROM events WHERE type='metering_failed'")
    booked = w.store.q(
        "SELECT r.amount_usd a, SUM(l.amount_usd) l FROM reservations r"
        " JOIN ledger l ON l.reservation_id = r.id WHERE r.session_id IS NOT NULL"
        " GROUP BY r.id"
    )
    assert booked and all(b["l"] == pytest.approx(b["a"]) and b["a"] > 0 for b in booked)
    assert not w.store.q("SELECT 1 FROM sessions WHERE status='done'")
