"""Fixtures for the bench tests: a real GitHub App whose transport fails on any
request, and a runner wired to the real GitHub publisher and PR poll (a bench job
must never use them), with the stubbed sessions of the coord-loop tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.harness import LocalTestRunner
from tests.jobs.test_coord_loop import KEY, StandIn
from tests.test_devimage import FakeDocker, FakeSuites
from vigild.budget import Budget
from vigild.config import Config, RepoPolicy, Secrets
from vigild.contract.models import BenchInfo
from vigild.db import Store
from vigild.devimage import DevImages, SessionImages
from vigild.gateway.service import Gateway
from vigild.github.app import GitHubApp
from vigild.jobs.runner import GitHubPRs, GitHubPublisher, JobRunner
from vigild.jobs.service import JobService
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier
from vigild.workspace import Workspace

BENCH_REPO = "bench/fixture"
BENCH = BenchInfo(
    run_id="run-1", task_id="inst-1", dataset="polybench", config_hash="abc123", emulated=False
)


class NoGitHub:
    """A transport that fails the test on any request, and remembers it."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(f"{req.method} {req.url}")
        raise AssertionError(f"a bench job reached the network: {req.method} {req.url}")


def _pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def net() -> NoGitHub:
    return NoGitHub()


@pytest.fixture
def app(net: NoGitHub, store: Store) -> GitHubApp:
    http = httpx2.Client(transport=httpx2.MockTransport(net))
    a = GitHubApp(
        "1", _pem(), http=http, claude_mods="o/mods", allowed_repos=["o/real", BENCH_REPO]
    )
    a.bind_jobs(lambda job_id: store.get_job(job_id).source)
    return a


@pytest.fixture
def world(
    cfg: Config, tmp_path: Path, fixture_repo: dict[str, Path], app: GitHubApp, store: Store
) -> Iterator[dict[str, Any]]:
    cfg.repos.repos[BENCH_REPO] = RepoPolicy(
        decider=["haiku"],
        backends=["claude-sonnet", "claude-opus", "claude-haiku"],
        dev_dockerfile="images/dev/vigil.Dockerfile",
        dev_lockfiles=["uv.lock"],
        dev_suites={"smoke": "true"},
        dev_image_tools=["git", "grep", "pytest"],
    )
    budget = Budget(store, cfg.budgets)
    gateway = Gateway(cfg, store, budget, Secrets({}))
    ws = Workspace(
        tmp_path / "data",
        remote_url=lambda r: str(fixture_repo["bare"]),
        token_for=lambda repo: app.lease(repo, {"contents": "read"}),
    )
    # slice 1b imports a task image's /app; here the mirror is made by hand
    mirror = ws._mirror_path(BENCH_REPO)
    mirror.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--mirror", "--quiet", str(fixture_repo["bare"]), str(mirror)], check=True
    )
    images = DevImages(cfg, store, FakeDocker(), ws, FakeSuites())
    standin = StandIn()
    runner = JobRunner(
        cfg, store, budget, gateway, standin, ws,
        GitHubPublisher(app, app.http, ws),  # the real one: a bench job must never use it
        Telemetry(), images, Verifier(ws, LocalTestRunner(), host_key=KEY),
        background=False, session_images=SessionImages(images, lambda: tmp_path),
        pr_state=GitHubPRs(app).state,
    )  # fmt: skip
    images.on_failure = runner.dev_image_failed
    assert runner.session_images is not None
    runner.session_images.on_failure = runner.dev_image_failed
    # ADR-0004 rule 5: images are built in an explicit phase before any session or
    # grader runs (slice 1b's loader does this for real tasks); here, by hand
    base = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    runner.session_images.ensure_fresh(BENCH_REPO, base)
    jobs = JobService(cfg, store, runner)
    runner.jobs = jobs
    yield {"jobs": jobs, "runner": runner, "store": store, "standin": standin, "budget": budget}
