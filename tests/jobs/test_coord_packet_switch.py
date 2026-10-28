"""`routing.coord_packet`: classic (today's packet) or lean (ADR-0005 part 1)."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.jobs.test_coord_loop import World, _approve, auth, world  # noqa: F401
from vigild import config as config_mod
from vigild.config import Config, Routing
from vigild.jobs.model import Role
from vigild.packets.assemble import COORD_PACKETS, assemble_coord_classic, assemble_coord_lean

INDEX_HEADING = "# 1a. Feature-map index"


def _first_coord_packet(w: World) -> str:
    _approve(w)
    return next(s.packet for s in w.standin.specs if s.role is Role.COORD)


def test_the_default_is_classic(cfg: Config) -> None:
    assert cfg.routing.coord_packet == "classic"


def test_both_variants_are_registered_by_name() -> None:
    assert {"classic": assemble_coord_classic, "lean": assemble_coord_lean} == COORD_PACKETS


def test_an_unknown_value_fails_config_load() -> None:
    raw = config_mod.load().routing.model_dump()
    with pytest.raises(ValidationError):
        Routing.model_validate({**raw, "coord_packet": "tiny"})
    with pytest.raises(ValidationError):
        config_mod.load(overrides={"routing": {**raw, "coord_packet": "tiny"}})


def test_the_runner_sends_the_classic_packet_by_default(world: World) -> None:
    packet = _first_coord_packet(world)
    assert INDEX_HEADING not in packet
    assert "## docs/feature-map.md" in packet


def test_lean_selects_the_lean_packet(world: World) -> None:
    world.runner.cfg.routing.coord_packet = "lean"
    packet = _first_coord_packet(world)
    assert INDEX_HEADING in packet
    assert "## docs/feature-map.md" not in packet
