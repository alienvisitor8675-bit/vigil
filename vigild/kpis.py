"""The KPIs that drive the trust ladder, routing and the cost model (owner
decisions 2026-10-22). Their raw data is stored from phase 1 (job state
events with timestamps, per-session cost, fix-job lineage, review rounds);
each KPI is computed where data exists and is None until then.

- first_pass_merge_rate: merged jobs that never went back from in_review to
  building, over merged jobs.
- findings_per_pr: review findings per PR, by severity (reviews: phase 2).
- revert_fix_job_rate: fix jobs opened against shipped jobs, over shipped jobs.
- cost_per_merged_pr: metered spend of merged jobs' sessions, per merged PR.
- median_spec_to_merge_hours: job creation to the `merged` transition.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

from vigild.db import Store

KPIS = (
    "first_pass_merge_rate",
    "findings_per_pr",
    "revert_fix_job_rate",
    "cost_per_merged_pr",
    "median_spec_to_merge_hours",
)


def _transitions(store: Store) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for r in store.q("SELECT job_id, ts, data FROM events WHERE type='state' ORDER BY id"):
        d = json.loads(r["data"])
        out.setdefault(r["job_id"], []).append((d["from"], d["to"], r["ts"]))
    return out


def compute(store: Store) -> dict[str, Any]:
    trans = _transitions(store)
    merged = {j: t for j, t in trans.items() if any(to == "merged" for _, to, _ in t)}
    result: dict[str, Any] = dict.fromkeys(KPIS)
    if merged:
        first_pass = [
            j
            for j, t in merged.items()
            if not any(f == "in_review" and to == "building" for f, to, _ in t)
        ]
        result["first_pass_merge_rate"] = len(first_pass) / len(merged)
        prs = 0
        cost = 0.0
        for j in merged:
            rows = store.q("SELECT cost_usd, pr_url FROM sessions WHERE job_id=?", (j,))
            cost += sum(r["cost_usd"] for r in rows)
            prs += len({r["pr_url"] for r in rows if r["pr_url"]})
        result["cost_per_merged_pr"] = cost / prs if prs else None
        hours = []
        for j, t in merged.items():
            created = store.get_job(j).created_at
            merged_at = next(ts for _, to, ts in t if to == "merged")
            delta = datetime.fromisoformat(merged_at) - datetime.fromisoformat(created)
            hours.append(delta.total_seconds() / 3600)
        result["median_spec_to_merge_hours"] = statistics.median(hours)
        fixes = sum(len(store.child_jobs(j)) for j in merged)
        result["revert_fix_job_rate"] = fixes / len(merged)
    return result
