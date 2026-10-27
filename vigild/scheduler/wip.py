"""The WIP limit (design "Intake", rule 2; plan 2a, item 2): at most
`budgets.wip_per_repo` open fleet PRs per repo. Merging, not spawning, is the
bottleneck: at the limit, dispatch of anything that would open a new PR
pauses, and the inbox shows the stack waiting on you.

A PR counts from the moment its task starts building (it will open one)
until vigild sees it merged or closed, or its job ends.
"""

from __future__ import annotations

import json
from typing import Any

from vigild.db import Store

GONE_TASK = ("merged", "closed", "superseded", "abandoned")
CLOSED_JOB = ("merged", "verifying", "done", "abandoned")


def pr_of(store: Store, task_id: str) -> str | None:
    ev = store.q(
        "SELECT data FROM events WHERE task_id=? AND type='pr_open' ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return str(json.loads(ev[0]["data"])["pr_url"]) if ev else None


def stack(store: Store, repo: str) -> list[dict[str, Any]]:
    """The repo's open fleet PRs, and tasks building toward one, oldest first."""
    rows = store.q(
        "SELECT t.id, t.job_id, t.state, j.state AS job_state FROM tasks t"
        " JOIN jobs j ON j.id = t.job_id WHERE j.repo=? ORDER BY t.created_at, t.id",
        (repo,),
    )
    out = []
    for r in rows:
        if r["state"] in GONE_TASK or r["job_state"] in CLOSED_JOB:
            continue
        url = pr_of(store, r["id"])
        if url or r["state"] == "running":
            out.append(
                {"task_id": r["id"], "job_id": r["job_id"], "state": r["state"], "pr_url": url}
            )
    return out


def room(store: Store, repo: str, limit: int) -> int:
    """How many more tasks may start toward a new PR in this repo."""
    return max(0, limit - len(stack(store, repo)))


def opens_a_pr(store: Store, task_id: str) -> bool:
    """A task without a PR yet: starting it takes a place in the stack."""
    return pr_of(store, task_id) is None
