"""The task contract: one schema in, one out, one event stream, one report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vigild.contract import export
from vigild.contract.models import Event, Result, Task, WorkerReport
from vigild.provenance import Provenance

ROOT = Path(__file__).resolve().parents[2]

DESIGN_TASK = {
    "task_id": "pa-0142",
    "repo": "thomasbardhi01/parkagent",
    "base": "main",
    "branch": "vigil/pa-0142-rate-limit-tests",
    "tier": "local",
    "tier_reason": "label:tests + paths:tests/** + size:S",
    "backend": {
        "kind": "openai_compatible",
        "base_url": "http://tower:8080/v1",
        "model": "qwen3.6-35b-a3b",
        "key_ref": "LLAMA_API_KEY",
    },
    "why": "rate limiting has no tests; #139 changed it",
    "value": 4,
    "risk": 2,
    "spec": {
        "goal": "Add tests for rate limiting on /api/park",
        "test_cmd": "pytest tests/api/test_rate_limit.py -q",
        "must_fail_first": True,
        "files_in_scope": ["tests/api/test_rate_limit.py", "docs/feature-map.md"],
        "done_when": "test_cmd exits 0; docs/feature-map.md lists the limit",
        "out_of_scope": ["src/billing/**", "src/auth/**"],
    },
    "limits": {"max_turns": 60, "max_minutes": 40, "max_cost_usd": 4.00},
    "depends_on": ["pa-0139"],
    "attempt": 1,
}

DESIGN_RESULT = {
    "task_id": "pa-0142",
    "status": "pr_open",
    "branch": "vigil/pa-0142-rate-limit-tests",
    "pr_url": "https://github.com/x/y/pull/312",
    "test_before": "fail",
    "test_after": "pass",
    "files_changed": ["tests/api/test_rate_limit.py", "docs/feature-map.md"],
    "summary": "3 tests; 429 after 10 req/min; docs updated",
    "usage": {
        "model": "qwen3.6-35b-a3b-q4",
        "tokens_in": 48210,
        "tokens_out": 6120,
        "tokens_cache_read": 31000,
        "tokens_cache_write": 0,
        "cost_usd": 0,
        "gpu_seconds": 1130,
        "energy_j": 312000,
    },
    "trace_id": "lf-9c1d",
    "provenance": "local",
}


def test_design_task_example_validates() -> None:
    t = Task.model_validate(DESIGN_TASK)
    assert t.spec.test_cmd and t.limits.max_cost_usd == 4.0


def test_design_result_example_validates() -> None:
    r = Result.model_validate(DESIGN_RESULT)
    assert r.provenance is Provenance.LOCAL


def test_task_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate({**DESIGN_TASK, "surprise": 1})


@pytest.mark.parametrize("field,bad", [("value", 0), ("value", 6), ("risk", 0), ("risk", 9)])
def test_scores_are_one_to_five(field: str, bad: int) -> None:
    with pytest.raises(ValidationError):
        Task.model_validate({**DESIGN_TASK, field: bad})


def test_backend_key_ref_is_a_name_not_a_value() -> None:
    bad = {**DESIGN_TASK, "backend": {**DESIGN_TASK["backend"], "key_ref": "sk-ant-api03-abc"}}  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        Task.model_validate(bad)


def test_event_types_are_closed() -> None:
    Event.model_validate(
        {"session_id": "s1", "type": "tool_call", "data": {"name": "Bash"}, "provenance": "claude"}
    )
    with pytest.raises(ValidationError):
        Event.model_validate(
            {"session_id": "s1", "type": "chat", "data": {}, "provenance": "claude"}
        )


def test_worker_report_needs_all_five_self_check_answers() -> None:
    report = {
        "task_id": "pa-0001-w1",
        "job_id": "pa-0001",
        "test_before": "fail",
        "test_after": "pass",
        "files_changed": ["docs/a.md"],
        "summary": "did it",
        "self_check": {
            "test_failed_first": True,
            "outside_scope": [],
            "not_done": "nothing",
            "reviewer_would_flag": "naming",
            "could_delete": "nothing",
        },
        "open_questions": [],
        "provenance": "claude",
    }
    WorkerReport.model_validate(report)
    del report["self_check"]["could_delete"]  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        WorkerReport.model_validate(report)


def test_every_model_carries_provenance() -> None:
    for model in (Result, Event, WorkerReport):
        assert "provenance" in model.model_json_schema()["required"]


def test_committed_schemas_match_the_models() -> None:
    for name, schema in export.schemas().items():
        path = ROOT / "schemas" / name
        assert path.exists(), f"run `uv run python -m vigild.contract.export` ({name} missing)"
        assert json.loads(path.read_text()) == schema, f"{name} is stale; re-export"
