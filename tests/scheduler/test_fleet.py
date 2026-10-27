"""The fleet in the runner (plan 2a, item 2): sessions at once across jobs, a
job's tasks as a dependency graph, and the WIP limit."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from tests.conftest import auth
from tests.jobs.test_coord_loop import World, _failure_item, _request
from tests.jobs.test_coord_loop import world as world
from vigild.executors.base import SessionSpec


def spec_n(n: int) -> str:
    acs = "\n".join(
        f"- AC{i}: doc {i} says done. test: `pytest -q tests/test_d{i}.py`" for i in range(1, n + 1)
    )
    files = "\n".join(f"- `docs/d{i}.md`\n- `tests/test_d{i}.py`" for i in range(1, n + 1))
    iface = ", ".join(f"`docs/d{i}.md` and `tests/test_d{i}.py`" for i in range(1, n + 1))
    return (
        "# Spec: docs\n\n## Goal\nEach doc says done.\n\n## Non-goals\n- Anything else.\n\n"
        f"## Acceptance criteria\n{acs}\n\n## Files in scope\n{files}\n\n"
        "## Files out of scope\n- `src/**`\n\n## Money paths\nnone\n\n"
        f"## Interfaces\n- None shared: each worker edits only its own files ({iface}).\n\n"
        "## Risks\n- None.\n\n## Rollback\nRevert the PRs.\n"
    )


def plan_n(job: str, n: int, deps: dict[int, list[int]] | None = None) -> str:
    workers = [
        {
            "task_id": f"{job}-w{i}",
            "goal": f"doc {i}",
            "why": f"AC{i}",
            "value": 3,
            "risk": 1,
            "size": "XS",
            "test_cmd": f"pytest -q tests/test_d{i}.py",
            "done_when": f"AC{i} holds",
            "files_in_scope": [f"docs/d{i}.md", f"tests/test_d{i}.py"],
            "labels": ["docs"],
            "max_cost_usd": 1.0,  # four at $2 each wouldn't fit the $5 day
            "depends_on": [f"{job}-w{d}" for d in (deps or {}).get(i, [])],
        }
        for i in range(1, n + 1)
    ]
    verify = [
        {"criterion": f"AC{i}", "level": 0, "why_not_higher": "a doc check has no property"}
        for i in range(1, n + 1)
    ]
    return (
        "# Plan\n\n## Notes addressed\n- keep it short: adopted\n\n## Workers\n```json\n"
        + json.dumps({"workers": workers, "verify": verify}, indent=1)
        + "\n```\n"
    )


def _doc_work(delay: float = 0.0) -> Any:
    def edit(spec: SessionSpec, task: dict[str, Any]) -> None:
        i = task["task_id"].rsplit("-w", 1)[1]
        time.sleep(delay)
        (spec.clone / "tests").mkdir(exist_ok=True)
        (spec.clone / f"tests/test_d{i}.py").write_text(
            "from pathlib import Path\n\n\ndef test_done():\n"
            f"    assert 'done' in Path('docs/d{i}.md').read_text()\n"
        )
        (spec.clone / "docs").mkdir(exist_ok=True)
        (spec.clone / f"docs/d{i}.md").write_text("done\n")

    return edit


def _run(w: World, n: int, deps: dict[int, list[int]] | None = None, delay: float = 0.0) -> str:
    w.standin.coord_drafts = [lambda j: (spec_n(n), plan_n(j, n, deps))]
    w.standin.worker_edit = _doc_work(delay)
    job = _request(w)
    (item,) = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    r = w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert r.status_code == 200, r.text
    return job


def test_three_sessions_at_once_across_the_fleet(world: World) -> None:
    w = world
    job = _run(w, 4, delay=0.3)
    assert w.store.get_job(job).state.value == "ready"
    assert w.standin.max_running == 3
    assert w.runner.slots.peak == 3


def test_a_diamond_job_runs_in_order(world: World) -> None:
    w = world
    order: list[tuple[str, str]] = []
    lock = threading.Lock()
    edit = _doc_work(0.05)

    def logged(spec: SessionSpec, task: dict[str, Any]) -> None:
        with lock:
            order.append(("start", task["task_id"][-2:]))
        edit(spec, task)
        with lock:
            order.append(("end", task["task_id"][-2:]))

    w.standin.coord_drafts = [lambda j: (spec_n(4), plan_n(j, 4, {2: [1], 3: [1], 4: [2, 3]}))]
    w.standin.worker_edit = logged
    job = _request(w)
    (item,) = w.client.get("/api/inbox", headers=auth(w.tokens.read)).json()["specs"]
    w.client.post(f"/api/inbox/{item['id']}/approve", headers=auth(w.tokens.admin), json={})
    assert w.store.get_job(job).state.value == "ready"
    at = order.index
    assert at(("end", "w1")) < at(("start", "w2")) and at(("end", "w1")) < at(("start", "w3"))
    assert at(("end", "w2")) < at(("start", "w4")) and at(("end", "w3")) < at(("start", "w4"))


def test_a_fifth_pr_waits(world: World) -> None:
    w = world
    assert w.runner.cfg.budgets.wip_per_repo == 4
    job = _run(w, 5)
    assert w.store.get_job(job).state.value == "blocked"
    assert len(w.publisher.prs) == 4
    waiting = [t.id for t in w.store.list_tasks(job) if t.state == "waiting"]
    assert len(waiting) == 1
    # the repo's WIP breaker (item 4) pages once, with the stack waiting on you
    item = _failure_item(w, None, "breaker")
    assert item["body"]["breaker_kind"] == "wip"
    assert item["body"]["detail"]["waiting"] == waiting
    assert len(item["body"]["detail"]["stack"]) == 4
    assert all(x["pr_url"] for x in item["body"]["detail"]["stack"])
    # one merges (vigild sees it through its PR poll, item 5); a reset runs the fifth
    merged = next(t.id for t in w.store.list_tasks(job) if t.state == "pr_open")
    w.store.update_task(merged, state="merged")
    r = w.client.post(f"/api/inbox/{item['id']}/reset", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert len(w.publisher.prs) == 5


def test_a_pr_being_recorded_still_holds_its_place(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#34. A worker marks its task `pr_open` and, a statement later, records the
    `pr_open` event with the URL. In that gap the task has no URL and is no longer
    `running`; the stack must still count it, or a dispatch pass that runs then sees
    room and starts the fifth task. Forced: w2 stops in the gap, and the pass that
    follows w1's finish checks the room only once w2 is there."""
    from vigild.scheduler import wip

    w = world
    in_gap, checked = threading.Event(), threading.Event()
    real_room = wip.room
    calls = 0

    def room(store: Any, repo: str, limit: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:  # the pass that follows the first worker's finish
            assert in_gap.wait(30)
        n = real_room(store, repo, limit)
        if calls == 2:
            checked.set()
        return n

    monkeypatch.setattr(wip, "room", room)
    real_add = w.store.add_event

    def add_event(type: str, data: Any, provenance: Any, **kw: Any) -> int:
        if type == "pr_open" and (kw.get("task_id") or "").endswith("-w2"):
            in_gap.set()
            assert checked.wait(30)
        return real_add(type, data, provenance, **kw)

    monkeypatch.setattr(w.store, "add_event", add_event)
    job = _run(w, 5)
    assert w.store.get_job(job).state.value == "blocked"
    assert len(w.publisher.prs) == 4


def test_a_task_turns_pr_open_and_records_its_pr_in_one_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#41 review: the gap #34 found is closed at its source as well. The task's
    `pr_open` state and the `pr_open` event with its URL are written in one
    transaction, so no reader sees a task that is `pr_open` without its PR."""
    w = world
    seen: list[bool] = []
    real = w.store.add_event

    def add_event(kind: str, *a: Any, **k: Any) -> Any:
        if kind == "pr_open":
            seen.append(w.store.conn.in_transaction)
        return real(kind, *a, **k)

    monkeypatch.setattr(w.store, "add_event", add_event)
    _run(w, 2)
    assert seen and all(seen), seen


def test_nothing_commits_between_pr_open_and_its_pr_event(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two writes share one transaction (#41 review, round 2): from the task's
    `pr_open` state write to its PR event, the connection never commits, so no
    other reader can see one without the other."""
    w = world
    trace: list[str] = []
    w.store.conn.set_trace_callback(trace.append)
    try:
        _run(w, 2)
    finally:
        w.store.conn.set_trace_callback(None)
    starts = [i for i, sql in enumerate(trace) if "UPDATE tasks" in sql and "pr_open" in sql]
    assert starts, "no pr_open state write was traced"
    for i in starts:
        end = next(j for j in range(i, len(trace)) if "INSERT INTO events" in trace[j]
                   and "pr_open" in trace[j])  # fmt: skip
        assert not any(sql.strip().upper() == "COMMIT" for sql in trace[i:end]), trace[i:end]
        # and the state write itself is inside an explicit transaction, not autocommit
        marks = [sql.strip().upper() for sql in trace[:i]
                 if sql.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]  # fmt: skip
        assert marks and marks[-1].startswith("BEGIN"), marks[-3:]
