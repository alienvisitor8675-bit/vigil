"""Shared fixtures. No fixture holds a real secret: tokens are generated per test."""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vigild import config as config_mod
from vigild.api.app import create_app
from vigild.config import Config
from vigild.db import Store
from vigild.jobs.service import JobService
from vigild.services import Services, Tokens


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = config_mod.load()
    c.vigild.data_dir = str(tmp_path / "data")
    # The cap-mechanics tests are written against a $5 day (two $2 workers fit,
    # a third queues); the real value is asserted in tests/test_budget.py.
    c.budgets.daily_usd = 5.0
    c.budgets.monthly_usd = 100.0  # the tests' own month, whatever the real cap
    return c


@pytest.fixture
def store() -> Store:
    return Store.open(":memory:")


@dataclass
class RecordingRunner:
    created: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    rebuilt: list[tuple[str, str]] = field(default_factory=list)

    def on_job_created(self, job_id: str, needs_spec: bool) -> None:
        self.created.append(job_id)

    def on_approved(self, job_id: str) -> None:
        self.approved.append(job_id)

    def on_resume(self, job_id: str, *, replan: bool = False) -> None:
        self.resumed.append(job_id)

    def on_rebuild(self, repo: str, base_sha: str) -> None:
        self.rebuilt.append((repo, base_sha))

    def busy(self, job_id: str) -> bool:
        return False


@pytest.fixture
def tokens() -> Tokens:
    return Tokens(
        admin=secrets.token_hex(16), read=secrets.token_hex(16), heartbeat=secrets.token_hex(16)
    )


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def services(cfg: Config, store: Store, tokens: Tokens, runner: RecordingRunner) -> Services:
    return Services(cfg=cfg, store=store, tokens=tokens, jobs=JobService(cfg, store, runner))


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    """The loopback listener: bearer tokens only, no UI routes."""
    with TestClient(create_app(services)) as c:
        yield c


@pytest.fixture
def ui_client(services: Services) -> Iterator[TestClient]:
    """The Tailscale listener: the UI's cookie login as well (P7)."""
    with TestClient(create_app(services, ui=True)) as c:
        yield c


def auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# The session image sets a system core.hooksPath whose pre-push refuses every
# push; tests push to test-local repos, so every test git call turns hooks off.
NO_HOOKS = ("-c", "core.hooksPath=")


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *NO_HOOKS, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> dict[str, Any]:
    """A bare 'remote' standing in for GitHub, seeded with a small repo."""
    bare = tmp_path / "remote" / "fixture.git"
    bare.parent.mkdir(parents=True)
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    git(seed, "config", "user.email", "t@vigil.invalid")
    git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("# fixture\n")
    (seed / "CLAUDE.md").write_text("# Fixture rules\nRun `grep` checks.\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "feature-map.md").write_text("# Feature map\n")
    (seed / "docs" / "runbook.md").write_text("# Runbook\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))  # no push
    git(seed, "remote", "add", "origin", str(bare))
    return {"bare": bare, "seed": seed}


@pytest.fixture(autouse=True)
def public_repos() -> Iterator[None]:
    """vigild reads visibility live from GitHub (owner, 2026-10-23); tests see
    every repo as public unless they set their own source. Without one,
    vigild treats a repo as private."""
    from vigild.repos import set_visibility_source

    set_visibility_source(lambda repo: False)
    yield
    set_visibility_source(None)


def hold_usd(gw: Any, session_id: str, usd: float, model: str = "claude-sonnet-5-5") -> float:
    """Hold a call of about `usd` against a session, as one in flight: output tokens only
    (no input), the session's output cap lifted so any amount can be asked for. Returns
    the amount held; the gateway's clamp and reviewer allowance apply as for a real call."""
    from vigild.metering.calls import CallUsage

    rate = gw.prices.cost(model, CallUsage(tokens_out=1_000_000)) / 1_000_000
    n = max(1, round(usd / rate))
    gw.cfg.budgets.session_max_output_tokens = max(gw.cfg.budgets.session_max_output_tokens, n)
    body = {"model": model, "max_tokens": n}
    amount, _body, _raw = gw._hold_clamped(session_id, model, body, b"")
    return float(amount)


@pytest.fixture(autouse=True)
def no_host_backup_plist(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vigil doctor` reads the installed launchd plist for the backup
    destination; tests never see the host's plist or VIGIL_BACKUP_DIR."""
    from vigild import doctor

    monkeypatch.setattr(doctor, "BACKUP_PLIST", tmp_path_factory.mktemp("la") / "absent.plist")
    monkeypatch.delenv("VIGIL_BACKUP_DIR", raising=False)
