"""The trust ladder (plan 2a, item 1): every shape starts where
configs/review.yaml says; audits move it, weighted by verify level; money,
auth and consent never leave rung 0, and deps never passes rung 1."""

from __future__ import annotations

from typing import Any

import pytest

from vigild.config import Config, ConfigError, ShapePolicy, validate
from vigild.contract.models import Audit, AuditFinding, Shape, Task
from vigild.db import Store
from vigild.review.ladder import Ladder, classify_shape


def _audit(shape: Shape, rung: int, *, missed: str | None = None, n: int = 2) -> Audit:
    found: list[dict[str, Any]] = [
        {"severity": "medium", "file": "a.py", "line": i, "claim": "x", "fix": "y", "matched": i}
        for i in range(n)
    ]
    if missed:
        found.append({"severity": missed, "file": "b.py", "line": 9, "claim": "z", "fix": "w"})
    return Audit(
        task_id="t-w1",
        job_id="t",
        head="a" * 40,
        shape=shape,
        rung=rung,
        auditor="claude-opus",
        provenance="claude",  # type: ignore[arg-type]
        findings=[AuditFinding.model_validate(f) for f in found],
    )


@pytest.fixture
def ladder(cfg: Config, store: Store) -> Ladder:
    return Ladder(store, cfg, rng=lambda: 0.99)


def test_every_shape_starts_where_the_config_says(ladder: Ladder) -> None:
    assert ladder.rung(Shape.DOCS) == 0
    assert ladder.rung(Shape.FEATURE) == 0
    assert ladder.rung(Shape.DEPS) == 1
    # rung 0 audits every review
    assert ladder.audit_rate(Shape.DOCS, 1) == 1.0
    assert ladder.should_audit(Shape.DOCS, 0)


def _changes(store: Store, shape: Shape) -> list[Any]:
    return store.q("SELECT * FROM ladder_changes WHERE shape=? ORDER BY id", (shape.value,))


@pytest.fixture
def evaluated(cfg: Config) -> Config:
    """A passed local-reviewer eval: the only thing that lets a shape past rung 1."""
    cfg.review.local_reviewer_eval = "evals/results.csv@test"
    return cfg


def test_rung_zero_never_graduates(ladder: Ladder, store: Store) -> None:
    """design.md, the ladder table: rung 0 "Graduates when: n/a"."""
    for _ in range(200):
        assert ladder.record(_audit(Shape.DOCS, 0), verify_level=3) is None
    assert ladder.rung(Shape.DOCS) == 0 and _changes(store, Shape.DOCS) == []


def test_no_shape_passes_rung_one_before_the_local_reviewer_eval(
    ladder: Ladder, store: Store
) -> None:
    """design.md: "a reviewer enters rung 1 only if it matches >= 80% of the
    auditor's findings on the eval set". Until that eval exists every rung
    keeps Sonnet reviewing and Opus auditing every review."""
    ladder.set_rung(Shape.FEATURE, 1, reason="owner, after review")
    for _ in range(200):
        ladder.record(_audit(Shape.FEATURE, 1), verify_level=3)
    assert ladder.rung(Shape.FEATURE) == 1
    assert ladder.presets(Shape.FEATURE) == ("claude-sonnet", "claude-opus")
    assert ladder.audit_rate(Shape.FEATURE, 1) == 1.0
    refused = [e.data for e in store.events() if e.type == "ladder_refused"]
    assert ladder.set_rung(Shape.FEATURE, 2, reason="an attempt") is None
    assert ladder.rung(Shape.FEATURE) == 1
    refused = [e.data for e in store.events() if e.type == "ladder_refused"]
    assert refused and "eval" in refused[-1]["why"]


def test_a_promotion_writes_its_row_with_the_evidence(
    evaluated: Config, ladder: Ladder, store: Store
) -> None:
    need = evaluated.review.rungs[1].promote_after
    assert need is not None
    ladder.set_rung(Shape.DOCS, 1, reason="owner, after review")
    # a level-0 audit counts half a PR: `need` of them aren't enough
    for _ in range(need):
        ladder.record(_audit(Shape.DOCS, 1), verify_level=0)
    assert ladder.rung(Shape.DOCS) == 1
    for _ in range(need):
        change = ladder.record(_audit(Shape.DOCS, 1), verify_level=0)
    assert change is not None and (change.frm, change.to) == (1, 2)
    row = _changes(store, Shape.DOCS)[-1]
    assert (row["old_rung"], row["new_rung"]) == (1, 2)
    assert row["sample_size"] == 2 * need and row["match_rate"] == pytest.approx(1.0)
    assert "audits" in row["reason"] and row["ts"]


def test_a_demotion_writes_its_row(evaluated: Config, ladder: Ladder, store: Store) -> None:
    ladder.set_rung(Shape.FEATURE, 2, reason="test setup")
    change = ladder.record(_audit(Shape.FEATURE, 2, missed="high"), verify_level=1)
    assert change is not None and change.frm == 2 and change.to < 2
    assert ladder.rung(Shape.FEATURE) == change.to
    row = _changes(store, Shape.FEATURE)[-1]
    assert (row["old_rung"], row["new_rung"], row["sample_size"]) == (2, change.to, 1)
    assert "missed high" in row["reason"] and row["match_rate"] is not None
    # every move is an event too, and every rung change has its row
    moves = [e.data for e in store.events() if e.type == "ladder"]
    assert len(moves) == len(_changes(store, Shape.FEATURE))


def test_a_missed_medium_finding_at_rung_three_drops_to_one(
    evaluated: Config, ladder: Ladder
) -> None:
    ladder.set_rung(Shape.DOCS, 3, reason="test setup")
    ladder.record(_audit(Shape.DOCS, 3, missed="medium"), verify_level=1)
    assert ladder.rung(Shape.DOCS) == 1


def test_a_missed_finding_resets_the_promotion_window(evaluated: Config, ladder: Ladder) -> None:
    need = evaluated.review.rungs[1].promote_after
    assert need is not None
    ladder.set_rung(Shape.REFACTOR, 1, reason="test setup")
    for _ in range(need - 1):
        ladder.record(_audit(Shape.REFACTOR, 1), verify_level=1)
    ladder.record(_audit(Shape.REFACTOR, 1, missed="medium"), verify_level=1)
    ladder.record(_audit(Shape.REFACTOR, 1), verify_level=1)
    assert ladder.rung(Shape.REFACTOR) == 1


@pytest.mark.parametrize("shape", [Shape.MONEY, Shape.AUTH, Shape.CONSENT])
def test_a_locked_shape_cant_be_promoted_and_the_refusal_is_logged(
    evaluated: Config, ladder: Ladder, store: Store, shape: Shape
) -> None:
    for _ in range(200):
        ladder.record(_audit(shape, 0), verify_level=3)
    assert ladder.rung(shape) == 0
    assert ladder.set_rung(shape, 2, reason="an attempt") is None
    assert ladder.rung(shape) == 0 and _changes(store, shape) == []
    refused = [e.data for e in store.events() if e.type == "ladder_refused"]
    assert refused[-1]["shape"] == shape.value and refused[-1]["to"] == 2
    assert ladder.audit_rate(shape, 3) == 1.0


def test_deps_never_passes_rung_one(evaluated: Config, ladder: Ladder, store: Store) -> None:
    for _ in range(200):
        ladder.record(_audit(Shape.DEPS, 1), verify_level=3)
    assert ladder.rung(Shape.DEPS) == 1
    assert ladder.set_rung(Shape.DEPS, 3, reason="an attempt") is None
    assert ladder.rung(Shape.DEPS) == 1
    assert [e.data["shape"] for e in store.events() if e.type == "ladder_refused"] == ["deps"]


def test_the_api_returns_each_shapes_rung_and_last_change(
    client: Any, tokens: Any, services: Any
) -> None:
    from tests.conftest import auth

    lad = Ladder(services.store, services.cfg)
    lad.set_rung(Shape.FEATURE, 1, reason="owner, after review")
    lad.record(_audit(Shape.FEATURE, 1, missed="high"), verify_level=1)
    r = client.get("/api/review/ladder", headers=auth(tokens.read))
    assert r.status_code == 200
    by = {x["shape"]: x for x in r.json()["shapes"]}
    last = by["feature"]["last_change"]
    assert (by["feature"]["rung"], last["old_rung"], last["new_rung"]) == (0, 1, 0)
    assert last["sample_size"] == 1 and "missed high" in last["reason"]
    assert by["docs"]["last_change"] is None and by["money"]["locked"] is True
    assert r.json()["local_reviewer_eval"] is None


def test_a_config_that_loosens_a_permanent_rung_is_refused(cfg: Config) -> None:
    cfg.review.shapes["money"] = ShapePolicy(rung=0, max_rung=2)
    with pytest.raises(ConfigError, match="money"):
        validate(cfg)
    cfg.review.shapes["money"] = ShapePolicy(rung=0, max_rung=0)
    cfg.review.shapes["deps"] = ShapePolicy(rung=1, max_rung=3)
    with pytest.raises(ConfigError, match="deps"):
        validate(cfg)


def test_sampling_above_rung_one_is_partial_and_harder_at_level_zero(
    evaluated: Config, ladder: Ladder
) -> None:
    ladder.set_rung(Shape.DOCS, 3, reason="test setup")
    assert ladder.audit_rate(Shape.DOCS, 1) == pytest.approx(0.10)
    assert ladder.audit_rate(Shape.DOCS, 0) == pytest.approx(0.20)
    assert not ladder.should_audit(Shape.DOCS, 1)  # rng 0.99


def _task(files: list[str], labels: list[str] | None = None, shape: str | None = None) -> Task:
    return Task.model_validate(
        {
            "task_id": "t-w1",
            "repo": "o/x",
            "why": "w",
            "value": 3,
            "risk": 1,
            "labels": labels or [],
            "shape": shape,
            "spec": {"goal": "g", "test_cmd": "pytest -q", "done_when": "d",
                     "files_in_scope": files},
        }
    )  # fmt: skip


def test_shapes_from_paths_labels_and_the_contract(cfg: Config) -> None:
    assert classify_shape(_task(["src/billing/charge.py"]), cfg) is Shape.MONEY
    assert classify_shape(_task(["src/auth/login.py"], ["docs"]), cfg) is Shape.AUTH
    assert classify_shape(_task(["app/consent/banner.ts"]), cfg) is Shape.CONSENT
    assert classify_shape(_task(["docs/x.md"], ["docs"]), cfg) is Shape.DOCS
    assert classify_shape(_task(["tests/test_x.py"], ["tests"]), cfg) is Shape.TESTS
    assert classify_shape(_task(["pnpm-lock.yaml"], shape="deps"), cfg) is Shape.DEPS
    assert classify_shape(_task(["src/x.py"]), cfg) is Shape.FEATURE
    # a contract can't talk a money path out of its shape
    assert classify_shape(_task(["src/billing/x.py"], shape="docs"), cfg) is Shape.MONEY
