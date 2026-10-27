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

`decider_report` is the decider's own (owner ruling 6): per question type,
how its answers' confidence is spread and how many escalated to Haiku. The
owner reads it after the first 50 real ParkAgent decisions, before any
threshold moves (act 0.85, escalate 0.6).
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


DECIDER_KINDS = ("choice", "score", "noul")


def _bucket(p: float) -> str:
    lo = min(int(p * 10), 9) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def decider_report(store: Store) -> dict[str, Any]:
    """Per question type: answers (`n`), their confidence in tenths
    (`buckets`), how many escalated to Haiku, how many acted, and how many
    decisions got no answer at all."""
    out: dict[str, Any] = {
        k: {"n": 0, "buckets": {}, "escalated": 0, "acted": 0, "no_answer": 0}
        for k in DECIDER_KINDS
    }
    rows = store.q("SELECT kind, probability, backend, action FROM decisions ORDER BY id")
    for r in rows:
        kind = r["kind"]
        if kind not in out:
            continue
        k = out[kind]
        if r["backend"] == "no-answer":
            k["no_answer"] += 1
            continue
        k["n"] += 1
        b = _bucket(float(r["probability"]))
        k["buckets"][b] = k["buckets"].get(b, 0) + 1
        k["escalated"] += r["action"] == "escalated"
        k["acted"] += r["action"] == "act"
    for k in out.values():
        k["buckets"] = dict(sorted(k["buckets"].items()))
    return out
