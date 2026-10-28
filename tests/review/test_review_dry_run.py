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


def _no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP request to a GitHub host fails the test, at the transport."""
    import httpx2

    def refuse(self: Any, request: Any) -> Any:
        assert "github" not in request.url.host, f"a dry run called {request.url}"
        raise AssertionError(f"a dry run made an HTTP request: {request.url}")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx2.Client, "send", lambda self, request, **kw: refuse(self, request))


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
    w.store.conn.close()  # vigild is stopped while the tool runs: no -wal is left
    data = tmp_path / "data"
    before = _snapshot(data)

    def no_network(self: socket.socket, address: Any) -> None:
        raise AssertionError(f"a dry run opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    _no_github(monkeypatch)
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
    src_db = sqlite3.connect(f"file:{data / 'vigil.db'}?mode=ro&immutable=1", uri=True)
    assert src_db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0] == (
        "reviewed"
    )
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
    world.store.conn.close()
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


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_a_closed_wal_database_gets_no_new_file_in_the_data_dir(
    filed: None, world: World, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _approve(world)
    world.store.conn.close()  # cleanly: no -wal or -shm is left beside vigil.db
    data = tmp_path / "data"
    assert (data / "vigil.db").is_file()
    assert not (data / "vigil.db-wal").exists() and not (data / "vigil.db-shm").exists()
    before, snap = _files(data), _snapshot(data)
    _no_github(monkeypatch)
    dry_run(
        data_dir=data, job_id=job, task_id=f"{job}-w1", head=None, out=tmp_path / "o.json",
        cfg=cfg, secrets=Secrets({}), scratch=tmp_path / "scratch", private=False,
        wiring=_wiring(Reviewer({**CLEAN_REVIEW, "verdict": "clean"}), tmp_path),
    )  # fmt: skip
    assert _files(data) == before and _snapshot(data) == snap


def _spend(w: World, usd: float, *, day: str | None = None, task: str | None = None) -> None:
    from vigild.db import utcnow

    now = utcnow()
    w.store.x(
        "INSERT INTO ledger (reservation_id, session_id, task_id, amount_usd, day, month, ts)"
        " VALUES (NULL, NULL, ?, ?, ?, ?, ?)",
        (task, usd, day or now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.isoformat()),
    )


@pytest.mark.parametrize("cap", ["daily", "monthly", "per_task"])
def test_the_production_caps_apply_and_refuse_before_any_call(
    filed: None, world: World, cfg: Config, tmp_path: Path, cap: str
) -> None:
    job = _approve(world)
    task = f"{job}-w1"
    if cap == "daily":
        _spend(world, 4.80)
    elif cap == "monthly":  # earlier this month: the day is clear, the month is not
        _spend(world, 99.80, day="2000-01-01")
    else:
        _spend(world, 1.80, day="2000-01-01", task=task)
    world.store.conn.close()
    reviewer = Reviewer({**CLEAN_REVIEW, "verdict": "clean"})
    with pytest.raises(DryRunError, match=f"{cap} cap"):
        dry_run(data_dir=tmp_path / "data", job_id=job, task_id=task, head=None,
                out=tmp_path / "o.json", cfg=cfg, secrets=Secrets({}),
                scratch=tmp_path / "scratch", private=False,
                wiring=_wiring(reviewer, tmp_path))  # fmt: skip
    assert reviewer.specs == []


def test_the_dry_run_books_open_reservations_and_stays_under_its_run_cap(
    filed: None, world: World, cfg: Config, tmp_path: Path
) -> None:
    job = _approve(world)
    world.store.conn.close()
    reviewer = Reviewer({**CLEAN_REVIEW, "verdict": "clean"})
    result = dry_run(
        data_dir=tmp_path / "data", job_id=job, task_id=f"{job}-w1", head=None,
        out=tmp_path / "o.json", cfg=cfg, secrets=Secrets({}), scratch=tmp_path / "scratch",
        private=False, wiring=_wiring(reviewer, tmp_path),
    )  # fmt: skip
    assert result["run_cap_usd"] == cfg.budgets.review_session_usd == 0.40
    assert result["cost_recorded_in_production"] is False
    assert json.loads((tmp_path / "o.json").read_text())["cost_usd"] == result["cost_usd"]


def test_the_cost_line_is_printed_and_in_the_json(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool()
    result = {"job": "j", "task": "j-w1", "head": "a" * 40, "round": 1, "reviewer": "claude-sonnet",
              "cost_usd": 0.1234, "error": None, "verdict": "clean", "findings": [],
              "checklist": None, "cost_recorded_in_production": False}  # fmt: skip
    monkeypatch.setattr(tool, "dry_run", lambda **kw: result)
    rc = tool.main(["--data-dir", str(tmp_path), "--job", "j", "--task", "j-w1",
                    "--out", str(tmp_path / "o.json")])  # fmt: skip
    assert rc == 0
    assert (
        "COST $0.1234: NOT recorded in the production ledger; add it to your records"
        in capsys.readouterr().out.splitlines()
    )


def test_private_refuses_a_hosted_preset_and_public_refuses_a_private_only_repo(
    filed: None, world: World, cfg: Config, tmp_path: Path
) -> None:
    from tests.jobs.test_coord_loop import REPO

    job = _approve(world)
    world.store.conn.close()
    common: dict[str, Any] = {"data_dir": tmp_path / "data", "job_id": job,
                              "task_id": f"{job}-w1", "head": None, "out": tmp_path / "o.json",
                              "cfg": cfg, "secrets": Secrets({}), "scratch": tmp_path / "s",
                              "wiring": _wiring(Reviewer({}), tmp_path)}  # fmt: skip
    # the fixture repo lists hosted Claude backends: a private run refuses them
    result = dry_run(private=True, **common)
    assert result["error"] and "hosted backend" in result["error"]
    # a repo that lists only private-safe entries can't be called public
    pol = cfg.repos.repos[REPO]
    pol.backends, pol.decider, pol.worker_overflow = ["tower-fast"], ["jev"], []
    with pytest.raises(DryRunError, match="private-only"):
        dry_run(private=False, **common)
