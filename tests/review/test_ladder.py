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


def test_good_audits_promote_weighted_by_verify_level(cfg: Config, ladder: Ladder) -> None:
    need = cfg.review.rungs[0].promote_after
    assert need is not None
    # a level-0 audit counts half a PR: `need` of them aren't enough
    for _ in range(need):
        ladder.record(_audit(Shape.DOCS, 0), verify_level=0)
    assert ladder.rung(Shape.DOCS) == 0
    for _ in range(need):
        change = ladder.record(_audit(Shape.DOCS, 0), verify_level=0)
    assert ladder.rung(Shape.DOCS) == 1
    assert change is not None and (change.frm, change.to) == (0, 1)
    # at level 1 it takes `need` audits from the new rung
    for _ in range(need - 1):
        ladder.record(_audit(Shape.TESTS, 0), verify_level=1)
    assert ladder.rung(Shape.TESTS) == 0
    ladder.record(_audit(Shape.TESTS, 0), verify_level=1)
    assert ladder.rung(Shape.TESTS) == 1


def test_a_missed_high_finding_demotes_the_shape(ladder: Ladder, store: Store) -> None:
    ladder.set_rung(Shape.FEATURE, 2, reason="test setup")
    change = ladder.record(_audit(Shape.FEATURE, 2, missed="high"), verify_level=1)
    assert change is not None and change.frm == 2 and change.to < 2
    assert ladder.rung(Shape.FEATURE) == change.to
    # every move is an event
    kinds = [e.data.get("to") for e in store.events() if e.type == "ladder"]
    assert kinds[-1] == change.to


def test_a_missed_medium_finding_at_rung_three_drops_to_one(ladder: Ladder) -> None:
    ladder.set_rung(Shape.DOCS, 3, reason="test setup")
    ladder.record(_audit(Shape.DOCS, 3, missed="medium"), verify_level=1)
    assert ladder.rung(Shape.DOCS) == 1


def test_a_missed_finding_resets_the_promotion_window(cfg: Config, ladder: Ladder) -> None:
    need = cfg.review.rungs[0].promote_after
    assert need is not None
    for _ in range(need - 1):
        ladder.record(_audit(Shape.REFACTOR, 0), verify_level=1)
    ladder.record(_audit(Shape.REFACTOR, 0, missed="medium"), verify_level=1)
    ladder.record(_audit(Shape.REFACTOR, 0), verify_level=1)
    assert ladder.rung(Shape.REFACTOR) == 0


@pytest.mark.parametrize("shape", [Shape.MONEY, Shape.AUTH, Shape.CONSENT])
def test_money_auth_and_consent_never_leave_rung_zero(ladder: Ladder, shape: Shape) -> None:
    for _ in range(200):
        ladder.record(_audit(shape, 0), verify_level=3)
    assert ladder.rung(shape) == 0
    ladder.set_rung(shape, 2, reason="an attempt")
    assert ladder.rung(shape) == 0
    assert ladder.audit_rate(shape, 3) == 1.0


def test_deps_never_passes_rung_one(ladder: Ladder) -> None:
    for _ in range(200):
        ladder.record(_audit(Shape.DEPS, 1), verify_level=3)
    assert ladder.rung(Shape.DEPS) == 1
    ladder.set_rung(Shape.DEPS, 3, reason="an attempt")
    assert ladder.rung(Shape.DEPS) == 1


def test_a_config_that_loosens_a_permanent_rung_is_refused(cfg: Config) -> None:
    cfg.review.shapes["money"] = ShapePolicy(rung=0, max_rung=2)
    with pytest.raises(ConfigError, match="money"):
        validate(cfg)
    cfg.review.shapes["money"] = ShapePolicy(rung=0, max_rung=0)
    cfg.review.shapes["deps"] = ShapePolicy(rung=1, max_rung=3)
    with pytest.raises(ConfigError, match="deps"):
        validate(cfg)


def test_sampling_above_rung_one_is_partial_and_harder_at_level_zero(ladder: Ladder) -> None:
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
