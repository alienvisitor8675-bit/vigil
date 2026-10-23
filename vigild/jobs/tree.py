"""The job tree: job -> coord -> workers -> subagents -> reviewer -> auditor,
each node with its state, cost, tokens and PR; plus artifacts and lineage.
`GET /api/jobs/{id}/tree` returns exactly this."""

from __future__ import annotations

from typing import Any

from vigild.db import Store
from vigild.jobs.model import SessionRow


def _node(s: SessionRow) -> dict[str, Any]:
    return {
        "id": s.id,
        "role": s.role.value,
        "task_id": s.task_id,
        "status": s.status,
        "backend_preset": s.backend_preset,
        "branch": s.branch,
        "pr_url": s.pr_url,
        "cost_usd": round(s.cost_usd, 6),
        "tokens_in": s.tokens_in,
        "tokens_out": s.tokens_out,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "provenance": s.provenance,
        "children": [],
    }


def job_tree(store: Store, job_id: str) -> dict[str, Any]:
    job = store.get_job(job_id)
    sessions = store.list_sessions(job_id)
    nodes = {s.id: _node(s) for s in sessions}
    roots: list[dict[str, Any]] = []
    for s in sessions:
        parent = nodes.get(s.parent_session_id) if s.parent_session_id else None
        (parent["children"] if parent else roots).append(nodes[s.id])
    tasks = store.list_tasks(job_id)
    return {
        "job": {
            "id": job.id,
            "repo": job.repo,
            "base": job.base,
            "title": job.title,
            "state": job.state.value,
            "blocked_from": job.blocked_from.value if job.blocked_from else None,
            "fix_depth": job.fix_depth,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "cost_usd": round(sum(s.cost_usd for s in sessions), 6),
        },
        "lineage": {"parent": job.parent_job_id, "children": store.child_jobs(job_id)},
        "tasks": [
            {
                "id": t.id,
                "state": t.state,
                "tier": t.tier,
                "tier_reason": t.tier_reason,
                "backend_preset": t.backend_preset,
            }
            for t in tasks
        ],
        "sessions": roots,
        "artifacts": [
            {"name": a.name, "task_id": a.task_id, "provenance": a.provenance, "sha256": a.sha256}
            for a in store.list_artifacts(job_id)
        ],
    }
