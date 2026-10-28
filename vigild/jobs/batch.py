"""Batch (issue #54): the read view of a batch, its jobs' state and cost.

A batch is a name and an id; its jobs are ordinary jobs that carry `batch_id`. Nothing
here changes a job, the scheduler or the WIP limit.
"""

from __future__ import annotations

from typing import Any

from vigild.db import Store

MAX_JOBS = 10

# A job's money: what its sessions and tasks have spent (the ledger) and what they still
# hold (open reservations). The review stage's hold names neither a session nor a task,
# so it is not counted against a job until a review session draws from it.
_OF_JOB = "(session_id IN (SELECT id FROM sessions WHERE job_id=?) OR task_id IN (SELECT id FROM tasks WHERE job_id=?))"  # noqa: E501


def job_cost(store: Store, job_id: str) -> tuple[float, float]:
    """(spent, held) in USD for one job."""
    spent = store.q(
        f"SELECT COALESCE(SUM(amount_usd), 0) FROM ledger WHERE {_OF_JOB}",  # noqa: S608
        (job_id, job_id),
    )[0][0]
    held = store.q(
        f"SELECT COALESCE(SUM(amount_usd), 0) FROM reservations WHERE status='open' AND {_OF_JOB}",  # noqa: S608
        (job_id, job_id),
    )[0][0]
    return float(spent), float(held)


def batch_view(store: Store, batch_id: str) -> dict[str, Any]:
    """Per-job state and cost, and the batch total (spent plus held)."""
    batch = store.get_batch(batch_id)  # KeyError: no such batch
    jobs: list[dict[str, Any]] = []
    for job_id in store.batch_job_ids(batch_id):
        job = store.get_job(job_id)
        spent, held = job_cost(store, job_id)
        jobs.append(
            {
                "job_id": job_id,
                "repo": job.repo,
                "title": job.title,
                "state": job.state.value,
                "issue": job.issue,
                "spent_usd": round(spent, 6),
                "held_usd": round(held, 6),
                "total_usd": round(spent + held, 6),
            }
        )
    spent = sum(j["spent_usd"] for j in jobs)
    held = sum(j["held_usd"] for j in jobs)
    return {
        "batch_id": batch_id,
        "name": batch["name"],
        "created_at": batch["created_at"],
        "jobs": jobs,
        "cost": {
            "spent_usd": round(spent, 6),
            "held_usd": round(held, 6),
            "total_usd": round(spent + held, 6),
        },
    }
