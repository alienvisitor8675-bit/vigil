"""KPIs: defined now, raw data stored now, computed where data exists."""

from __future__ import annotations

import json

import pytest

from vigild.db import Store
from vigild.jobs.model import JobState, Role
from vigild.kpis import KPIS, compute


def _state(store: Store, job: str, frm: str, to: str) -> None:
    store.add_event("state", {"from": frm, "to": to}, "human", job_id=job)


def test_nothing_merged_means_no_numbers(store: Store) -> None:
    assert compute(store) == dict.fromkeys(KPIS)


def test_kpis_from_raw_data(store: Store) -> None:
    a = store.create_job(repo="o/r", base="main", title="a", request="r", notes=[])
    b = store.create_job(repo="o/r", base="main", title="b", request="r", notes=[])
    for j in (a.id, b.id):
        s = store.create_session(j, Role.WORKER)
        store.update_session(s.id, pr_url=f"https://x/{j}", cost_usd=1.5)
    for f, t in (("building", "in_review"), ("in_review", "ready"), ("ready", "merged")):
        _state(store, a.id, f, t)
    for f, t in (
        ("building", "in_review"),
        ("in_review", "building"),
        ("building", "in_review"),
        ("in_review", "ready"),
        ("ready", "merged"),
    ):
        _state(store, b.id, f, t)
    store.create_job(
        repo="o/r", base="main", title="fix", request="r", notes=[], parent_job_id=b.id
    )
    k = compute(store)
    assert k["first_pass_merge_rate"] == 0.5
    assert k["cost_per_merged_pr"] == pytest.approx(1.5)
    assert k["revert_fix_job_rate"] == 0.5
    assert k["median_spec_to_merge_hours"] is not None
    assert k["findings_per_pr"] is None  # reviews arrive in phase 2
    assert JobState.MERGED.value == "merged"
    assert json.dumps(k)
