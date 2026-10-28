"""The runner picks the coord's model by shape and records the rule (ADR-0005 part 3)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

from typing import Any

from tests.conftest import auth
from tests.jobs.test_coord_loop import REPO, World, _request, world  # noqa: F401
from vigild.jobs.model import Role


def _submit(w: World, **extra: Any) -> str:
    r = w.client.post(
        "/api/jobs",
        headers=auth(w.tokens.admin),
        json={
            "repo": REPO,
            "title": "Document the rate limit",
            "request": "The limit is undocumented.",
            "why": "support asks weekly",
            "value": 3,
            "risk": 1,
            **extra,
        },
    )
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


def _coord(w: World, job: str) -> Any:
    return [s for s in w.store.list_sessions(job) if s.role is Role.COORD][-1]


def test_an_unlabelled_job_gets_the_opus_coord_and_the_rule_is_recorded(world: World) -> None:
    job = _request(world)
    s = _coord(world, job)
    assert s.backend_preset == "claude-opus"
    assert s.detail["coord_reason"] == "coord:rule-default"
    (ev,) = world.store.q(
        "SELECT data FROM events WHERE job_id=? AND type='coord_routed' AND session_id=?",
        (job, s.id),
    )
    assert "coord:rule-default" in ev["data"] and "claude-opus" in ev["data"]


def test_a_docs_job_gets_the_sonnet_coord(world: World) -> None:
    job = _submit(world, labels=["docs"])
    s = _coord(world, job)
    assert s.backend_preset == "claude-sonnet"
    assert s.detail["coord_reason"] == "coord:rule-tests-docs"


def test_an_architecture_label_beats_docs(world: World) -> None:
    job = _submit(world, labels=["docs", "architecture"])
    s = _coord(world, job)
    assert (s.backend_preset, s.detail["coord_reason"]) == (
        "claude-opus",
        "coord:rule-architecture",
    )


def test_a_replan_to_attempt_three_escalates_to_opus(world: World) -> None:
    job = _submit(world, labels=["tests"])
    assert _coord(world, job).backend_preset == "claude-sonnet"
    for _ in range(2):  # attempt is 1 plus the replans so far
        world.store.add_event("replan", {}, "human", job_id=job)
    from vigild.router.rules import CoordShape, route_coord

    shape = world.runner.coord_shape(world.store.get_job(job))
    assert isinstance(shape, CoordShape) and shape.attempt == 3
    assert route_coord(world.runner.cfg, REPO, shape).reason == "coord:rule-escalation"


def test_a_request_naming_a_pinned_file_gets_the_opus_coord(world: World) -> None:
    world.runner.cfg.repos.repos[REPO].ladder_pinned_paths = ["vigild/gateway/**"]
    job = _submit(
        world,
        title="Tidy the gateway",
        request="Rename a helper in `vigild/gateway/service.py`.",
        labels=["tests"],
    )
    s = _coord(world, job)
    assert s.backend_preset == "claude-opus"
    assert s.detail["coord_reason"].startswith("coord:rule-pinned (vigild/gateway/service.py ~")


def test_paths_come_from_notes_and_an_explicit_scope_field(world: World) -> None:
    job = _request(world)
    shape = world.runner.coord_shape(world.store.get_job(job))
    assert shape.paths == []
    from vigild.router.rules import request_paths

    got = request_paths(
        {
            "title": "t",
            "request": "see src/auth/login.py",
            "notes": ["`fly.toml` too"],
            "files_in_scope": ["docs/a.md"],
        }
    )
    assert sorted(got) == ["docs/a.md", "fly.toml", "src/auth/login.py"]
