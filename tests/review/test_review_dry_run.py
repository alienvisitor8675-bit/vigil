"""The review dry-run tool (`scripts/review-dry-run.py`): the reviewer rerun on a
past task builds the packet a real review built, and has no effects."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.jobs.test_coord_loop import CLEAN_REVIEW, World, _approve
from tests.jobs.test_coord_loop import world as world
from tests.test_devimage import FakeDocker, FakeSuites
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.devimage import DevImages, SessionImages
from vigild.executors.base import SessionSpec
from vigild.jobs.model import Role
from vigild.reviewdry import DryRunError, Wiring, dry_run, render

ROOT = Path(__file__).resolve().parents[2]
FINDING = {"severity": "low", "file": "docs/feature-map.md", "line": 3,
           "claim": "AC1 lists three rows; the diff has two", "fix": "add the third"}  # fmt: skip


@pytest.fixture
def filed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`world` with its database in a file under its data dir, as vigild keeps it."""
    real = Store.open.__func__  # type: ignore[attr-defined]

    def opener(cls: type[Store], path: str | Path) -> Store:
        if path == ":memory:":
            (tmp_path / "data").mkdir(exist_ok=True)
            path = tmp_path / "data" / "vigil.db"
        return real(cls, path)  # type: ignore[no-any-return]

    monkeypatch.setattr(Store, "open", classmethod(opener))


class Reviewer:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.specs: list[SessionSpec] = []

    def run(self, spec: SessionSpec, gateway_token: str, on_event: Any) -> Any:
        from vigild.executors.base import ExecOutcome

        assert gateway_token.startswith("vgw_")
        self.specs.append(spec)
        return ExecOutcome("done", 0, {"status": "done", "structured_output": self.answer})


def _nonceless(packet: str) -> str:
    """The fence nonces are random per packet; everything else must match."""
    return re.sub(r"(<<<(?:UNTRUSTED|END) [a-z0-9_-]+) [0-9a-f]{16}>>>", r"\1 N>>>", packet)


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(p.relative_to(root)): (
            p.stat().st_size,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.name.endswith(".db") else "",
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _wiring(reviewer: Reviewer, tmp: Path) -> Wiring:
    def images(cfg: Config, store: Store, ws: Any) -> Any:
        dev = DevImages(cfg, store, FakeDocker(), ws, FakeSuites())
        return dev, SessionImages(dev, lambda: tmp)

    return Wiring(reviewer, images, serve_gateway=False)  # type: ignore[arg-type]


def _tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "review_dry_run", ROOT / "scripts/review-dry-run.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_builds_the_real_reviews_packet_and_has_no_effects(
    filed: None,
    world: World,
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world
    job = _approve(w)
    task = f"{job}-w1"
    (real,) = [s for s in w.standin.specs if s.role is Role.REVIEWER and str(s.task_id) == task]
    data = tmp_path / "data"
    before = _snapshot(data)
    total = w.store.conn.total_changes

    def no_network(self: socket.socket, address: Any) -> None:
        raise AssertionError(f"a dry run opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    reviewer = Reviewer({**CLEAN_REVIEW, "verdict": "clean", "findings": [FINDING]})
    out = tmp_path / "out" / "review.json"
    result = dry_run(
        data_dir=data, job_id=job, task_id=task, head=None, out=out, cfg=cfg,
        secrets=Secrets({}), scratch=tmp_path / "scratch", private=False,
        wiring=_wiring(reviewer, tmp_path),
    )  # fmt: skip

    # the packet is the one the real review sent
    (spec,) = reviewer.specs
    assert spec.role is Role.REVIEWER and _nonceless(spec.packet) == _nonceless(real.packet)
    assert spec.output_schema == real.output_schema and spec.model == real.model
    assert spec.max_turns == real.max_turns and spec.image == real.image
    # its verdict and findings are printed and written under --out
    assert result["verdict"] == "clean" and result["findings"] == [FINDING]
    assert json.loads(out.read_text())["findings"] == [FINDING]
    assert "AC1 lists three rows" in render(result)
    # the source data dir is untouched: the same files, bytes, sizes and times
    assert _snapshot(data) == before
    assert w.store.conn.total_changes == total
    assert w.store.get_task(task).state == "reviewed"
    # the scratch run made no inbox item and no PR event, and left the job as it was
    scratch_db = sqlite3.connect(tmp_path / "scratch" / "vigil.db")
    assert scratch_db.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0] == 0
    assert (
        scratch_db.execute("SELECT COUNT(*) FROM events WHERE type LIKE 'pr_%'").fetchone()[0] == 0
    )
    assert scratch_db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0] == (
        "reviewed"
    )


def test_it_never_writes_inside_the_data_dir(filed: None, world: World, tmp_path: Path) -> None:
    job = _approve(world)
    for kw in ({"out": tmp_path / "data" / "r.json"}, {"scratch": tmp_path / "data" / "s"}):
        args: dict[str, Any] = {"out": tmp_path / "o.json", "scratch": tmp_path / "s", **kw}
        with pytest.raises(DryRunError, match="inside the data dir"):
            dry_run(data_dir=tmp_path / "data", job_id=job, task_id=f"{job}-w1", head=None,
                    secrets=Secrets({}), wiring=_wiring(Reviewer({}), tmp_path), **args)  # fmt: skip


def test_a_bad_head_or_task_is_refused(filed: None, world: World, tmp_path: Path) -> None:
    job = _approve(world)
    common: dict[str, Any] = {"data_dir": tmp_path / "data", "out": tmp_path / "o.json",
                              "secrets": Secrets({}), "wiring": _wiring(Reviewer({}), tmp_path)}  # fmt: skip
    with pytest.raises(DryRunError, match="40-hex"):
        dry_run(job_id=job, task_id=f"{job}-w1", head="abc", **common)
    with pytest.raises(DryRunError, match="no task"):
        dry_run(job_id=job, task_id="nope", head=None, scratch=tmp_path / "s", **common)


def test_the_cli_reports_a_missing_database(tmp_path: Path, capsys: Iterator[Any]) -> None:
    rc = _tool().main(["--data-dir", str(tmp_path), "--job", "x-0001", "--task", "x-0001-w1",
                       "--out", str(tmp_path / "o.json")])  # fmt: skip
    assert rc == 2
