"""The coord loop with real containers: a Haiku coord and two Haiku workers,
each in its own hardened container behind its own proxy, every model call
through vigild's gateway with the vigil key, branches published to a local
bare repo by vigild from the host (pytest -m "docker and live").

Haiku stands in for Opus and Sonnet here only to keep the run cheap; the
routing is the configured one in production."""

from __future__ import annotations

import json
import socket
import subprocess
import tarfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from tests.conftest import auth, git
from tests.jobs.test_coord_loop import REPO, BarePublisher
from vigild.api.app import create_app
from vigild.config import RepoPolicy
from vigild.devimage import DevImages, DockerImages, SessionImages
from vigild.executors.runtime import DockerRuntime, DockerTestRunner
from vigild.gateway.app import create_gateway_app
from vigild.jobs.runner import JobRunner
from vigild.server import Vigild, build
from vigild.services import Tokens
from vigild.verifier import Verifier
from vigild.workspace import Workspace

pytestmark = [pytest.mark.docker, pytest.mark.live]


@pytest.fixture
def live(
    tmp_path: Path, fixture_repo: dict[str, Path], tokens: Tokens
) -> Iterator[tuple[Vigild, JobRunner, BarePublisher, TestClient]]:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8788)) == 0:
            pytest.skip("port 8788 is in use")
    import docker

    v = build(data_dir=tmp_path / "vdata")
    if "anthropic" in v.services.degraded:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    cfg = v.cfg
    for tier in cfg.routing.tiers:
        cfg.routing.tiers[tier] = "claude-haiku"
    cfg.routing.coord = cfg.routing.fallback = "claude-haiku"
    cfg.budgets.coord_session_usd = 0.30
    cfg.budgets.per_task_usd = 0.30
    cfg.repos.repos[REPO] = RepoPolicy(
        decider=["haiku"],
        backends=["claude-haiku"],
        dev_dockerfile="images/dev/fixture.Dockerfile",
        dev_lockfiles=["README.md"],
        dev_suites={"smoke": "python -m pytest --version"},
        dev_image_tools=["git", "grep", "python", "pytest"],
    )
    client = docker.from_env()
    ws = Workspace(tmp_path / "work", remote_url=lambda r: str(fixture_repo["bare"]))
    tests = DockerTestRunner(cfg, client)
    images = DevImages(cfg, v.store, DockerImages(client), ws, tests)
    images.ensure_fresh(REPO, ws.head_sha(REPO, "main"))
    # the session image: the executor layer over the fixture's dev image (round 3)
    mods = tmp_path / "claude-mods"

    def claude_mods() -> Path:
        if not mods.exists():
            src = Path.home() / "Documents" / "claude-mods"
            tar = tmp_path / "mods.tar"
            with tar.open("wb") as f:
                subprocess.run(
                    ["git", "-C", str(src), "archive", "--format=tar",
                     cfg.vigild.github.claude_mods_sha],
                    stdout=f, check=True,
                )  # fmt: skip
            mods.mkdir()
            with tarfile.open(tar) as t:
                t.extractall(mods, filter="data")
        return mods

    sessions = SessionImages(images, claude_mods)
    publisher = BarePublisher(ws)
    runner = JobRunner(
        cfg,
        v.store,
        v.budget,
        v.gateway,
        DockerRuntime(cfg, client),
        ws,
        publisher,
        v.telemetry,
        images,
        Verifier(ws, tests),
        background=False,
        session_images=sessions,
    )
    v.services.jobs.runner = runner
    runner.jobs = v.services.jobs
    v.services.tokens = tokens
    server = uvicorn.Server(
        uvicorn.Config(
            create_gateway_app(v.gateway), host="127.0.0.1", port=8788, log_level="warning"
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.1)
    with TestClient(create_app(v.services)) as c:
        yield v, runner, publisher, c
    server.should_exit = True
    t.join(timeout=10)


def test_two_workers_build_in_their_own_containers(
    live: tuple[Vigild, JobRunner, BarePublisher, TestClient],
    tokens: Tokens,
    fixture_repo: dict[str, Path],
) -> None:
    v, _, publisher, c = live
    r = c.post(
        "/api/jobs",
        headers=auth(tokens.admin),
        json={
            "repo": REPO,
            "title": "Document the rate limit",
            "request": (
                "Two independent doc edits. (1) Add a line containing the words 'rate limit' to "
                "docs/feature-map.md, with a pytest test tests/test_feature_map.py that reads "
                "the file and asserts it. (2) Add a line containing RATE_LIMIT to "
                "docs/runbook.md saying it is the env var that sets the limit, with a pytest "
                "test tests/test_runbook.py that asserts it. One worker per doc and its test, in "
                "parallel; each test_cmd is `pytest -q tests/<its test file>`. Keep max_cost_usd "
                "at 0.25 per worker."
            ),
            "notes": ["keep it to one line per file"],
            "why": "the limit is undocumented",
            "value": 3,
            "risk": 1,
        },
    )
    job = r.json()["id"]
    spend = lambda: sum(x["cost_usd"] for x in v.store.q("SELECT cost_usd FROM model_calls"))  # noqa: E731
    inbox = c.get("/api/inbox", headers=auth(tokens.read)).json()
    print(
        f"\ncoord done: ${spend():.4f}; specs={len(inbox['specs'])} pages={[q['title'] for q in inbox['questions']]}"
    )
    for q in inbox["questions"]:
        print("page:", q["title"], json.dumps(q["body"])[:1500])
    assert len(inbox["specs"]) == 1, inbox["questions"]
    item = inbox["specs"][0]
    c.post(f"/api/inbox/{item['id']}/approve", headers=auth(tokens.admin), json={})
    tree = c.get(f"/api/jobs/{job}/tree", headers=auth(tokens.read)).json()
    inbox = c.get("/api/inbox", headers=auth(tokens.read)).json()
    print(
        f"after build: ${spend():.4f}; state={tree['job']['state']}; "
        f"pages={[q['title'] for q in inbox['questions']]}"
    )
    for q in inbox["questions"]:
        print("page:", q["title"], json.dumps(q["body"])[:3000])
    for a in v.store.q("SELECT task_id, content FROM artifacts WHERE name='tests.json'"):
        print("tests.json", a["task_id"], a["content"][:3000])
    # one coord session per draft; the workers hang under the draft that passed
    coord = [r for r in tree["sessions"] if r["role"] == "coord"][-1]
    workers = coord["children"]
    print("workers:", [(w["id"], w["status"], w["pr_url"]) for w in workers])
    assert len(workers) == 2
    assert all(w["status"] == "pr_open" for w in workers)
    assert tree["job"]["state"] == "in_review"
    for p in publisher.prs:
        files = git(fixture_repo["bare"], "ls-tree", "-r", "--name-only", p["head"])
        assert "worker_report.json" in files
    assert spend() < 1.0
