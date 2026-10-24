"""Hidden tests reach only the vault (hotfix 5, owner addition; pytest -m docker).

A scripted author: its "model" output is fixed text that a real container
(the repo's session image, on the session's own network behind the egress
proxy, with the clone mounted as the real runtime mounts it) writes into the
clone. The harness's job after it (commit, bundle) is the stand-in's, as in
tests/jobs/test_coord_loop.py; everything after that is the real runner, the
real verifier (in the dev image) and the real workspace.

Then the job is built. Each worker runs in a real container from the repo's
session image with its real mounts, and a search of that container's whole
filesystem (the image, the clone, every mount) must find nothing of the
author's output. The branches in the remote, the PRs, the stored artifacts and
events, and the workers' clones on the host must find nothing either. The
author's output is in the vault, and in the author's own clone and out dir."""

from __future__ import annotations

import json
import shlex
import subprocess
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import git
from tests.jobs.test_coord_loop import (
    KEY,
    REPO,
    BarePublisher,
    StandIn,
    World,
    _approve,
    _flag_edit,
    _named,
)
from vigild.api.app import create_app
from vigild.budget import Budget
from vigild.config import Config, RepoPolicy, Secrets
from vigild.db import Store
from vigild.devimage import DevImages, DockerImages, SessionImages
from vigild.executors.base import EventSink, ExecOutcome, SessionSpec
from vigild.executors.runtime import DockerRuntime, DockerTestRunner
from vigild.gateway.service import Gateway
from vigild.jobs.model import Role
from vigild.jobs.runner import JobRunner
from vigild.jobs.service import JobService
from vigild.services import Services, Tokens
from vigild.telemetry.spans import Telemetry
from vigild.verifier import Verifier
from vigild.workspace import Workspace

pytestmark = pytest.mark.docker

MARKER = "HIDDEN-AUTHOR-OUTPUT-3f9c1d7a52"
CONTROL = "SEARCH-CONTROL-8be40a6c"
BODY = (
    f"# {MARKER}\nfrom vigil_hidden import check, run_subject\n\n\n"
    "def test_flag_env_is_set():\n"
    "    r = run_subject(['python3', 'tools/check.py'])\n"
    "    check(r['returncode'] == 0 and r['stdout'].strip() == 'ok', repr(r))\n"
)
NAME = "flag env is set by the runner"


class ScriptedRuntime:
    """The executor: author sessions write their fixed output from inside a real
    container; every worker session first has its real container searched."""

    def __init__(self, standin: StandIn, runtime: DockerRuntime) -> None:
        self.standin = standin
        self.runtime = runtime
        self.searches: list[tuple[str, int, str]] = []

    def search(self, spec: SessionSpec) -> None:
        # the control proves the search sees files in this container; the marker
        # is looked for in the image, the clone and every mount, but not in /proc
        find = "grep -rIlF -- {} / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev"
        cmd = (
            f"printf {CONTROL} > /tmp/control; "
            f"echo CONTROL=$({find.format(CONTROL)} 2>/dev/null | wc -l); "
            f"echo HITS=$({find.format(MARKER)} 2>/dev/null | wc -l)"
        )
        code, out = self.runtime.probe(spec, cmd, timeout=600)
        assert code == 0, out
        self.searches.append((str(spec.task_id), code, out))

    def run(self, spec: SessionSpec, token: str, sink: EventSink) -> ExecOutcome:
        if spec.role is Role.WORKER:
            self.search(spec)
        return self.standin.run(spec, token, sink)


def container_author(runtime: DockerRuntime) -> Callable[[SessionSpec, dict[str, Any]], None]:
    def write(spec: SessionSpec, task: dict[str, Any]) -> None:
        folder = f".vigil/{spec.job_id}/hidden/{spec.task_id}"
        manifest = {
            "test_cmd": f"pytest -q {folder}/test_hidden.py",
            "tests": [{"name": NAME, "category": "happy-path"}],
        }
        # the agent uid writes into the clone as a session would
        subprocess.run(["chmod", "-R", "a+rwX", str(spec.clone)], check=True)
        script = (
            f"mkdir -p {shlex.quote(folder)} && "
            f"printf %s {shlex.quote(BODY)} > {shlex.quote(folder)}/test_hidden.py && "
            f"printf %s {shlex.quote(json.dumps(manifest))} > {shlex.quote(folder)}.json"
        )
        code, out = runtime.probe(spec, script)
        assert code == 0, out
        assert (spec.clone / folder / "test_hidden.py").read_text() == BODY

    return write


def _claude_mods(cfg: Config, tmp_path: Path) -> Callable[[], Path]:
    mods = tmp_path / "claude-mods"

    def get() -> Path:
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

    return get


@pytest.fixture
def dworld(
    cfg: Config, tmp_path: Path, fixture_repo: dict[str, Path], tokens: Tokens
) -> Iterator[tuple[World, ScriptedRuntime]]:
    import docker

    try:
        client = docker.from_env()
        client.ping()
        for image in (cfg.vigild.images.claude_executor, cfg.vigild.images.egress_proxy):
            client.images.get(image)
    except Exception as e:
        pytest.fail(f"Docker or the vigil images are not ready (scripts/build-images.py): {e}")
    cfg.repos.repos[REPO] = RepoPolicy(
        visibility="public",
        decider=["haiku"],
        backends=["claude-sonnet", "claude-opus", "claude-haiku"],
        dev_dockerfile="images/dev/fixture.Dockerfile",
        dev_lockfiles=["README.md"],
        dev_suites={"smoke": "python -m pytest --version"},
        dev_image_tools=["git", "grep", "python", "pytest"],
        runner_config=["tools/check.py"],
    )
    store = Store.open(":memory:")
    budget = Budget(store, cfg.budgets)
    gateway = Gateway(cfg, store, budget, Secrets({}))
    ws = Workspace(tmp_path / "work", remote_url=lambda r: str(fixture_repo["bare"]))
    tests = DockerTestRunner(cfg, client)
    images = DevImages(cfg, store, DockerImages(client), ws, tests)
    images.ensure_fresh(REPO, ws.head_sha(REPO, "main"))
    standin, publisher = StandIn(), BarePublisher(ws)
    runtime = DockerRuntime(cfg, client)
    scripted = ScriptedRuntime(standin, runtime)
    standin.authors = [container_author(runtime)]
    standin.coord_drafts = [_named]
    standin.worker_edit = _flag_edit()
    runner = JobRunner(
        cfg, store, budget, gateway, scripted, ws, publisher, Telemetry(), images,
        Verifier(ws, tests, host_key=KEY), background=False,
        session_images=SessionImages(images, _claude_mods(cfg, tmp_path)),
    )  # fmt: skip
    images.on_failure = runner.dev_image_failed
    assert runner.session_images is not None
    runner.session_images.on_failure = runner.dev_image_failed
    jobs = JobService(cfg, store, runner)
    runner.jobs = jobs
    services = Services(cfg=cfg, store=store, tokens=tokens, jobs=jobs)
    with TestClient(create_app(services)) as c:
        yield (
            World(c, store, budget, standin, publisher, fixture_repo["bare"], tokens, images,
                  runner, tests),  # type: ignore[arg-type]
            scripted,
        )  # fmt: skip


def test_a_scripted_authors_output_reaches_only_the_vault(
    dworld: tuple[World, ScriptedRuntime],
) -> None:
    w, scripted = dworld
    job = _approve(w)
    assert w.store.get_job(job).state.value == "in_review", w.store.get_job(job).state

    # 1. the output is in the vault: a commit that is on no pushed branch
    art = json.loads(w.store.get_artifact(job, "hidden_tests.json").content)
    entry = art["tasks"][f"{job}-w1"]
    assert entry["status"] == "ok"
    ws = w.runner.workspace
    stored = ws.read_blob(REPO, entry["sha"], entry["files"][0])
    assert stored == BODY
    rec = json.loads(w.store.get_artifact(job, "tests.json", task_id=f"{job}-w1").content)
    assert rec["hidden"]["ok"] and rec["hidden"]["count"]["head"] == 1

    # 2. not in either worker's real container: not its image, clone or any mount
    assert sorted(t for t, _, _ in scripted.searches) == [f"{job}-w1", f"{job}-w2"]
    for task, _, out in scripted.searches:
        assert "CONTROL=1" in out and "HITS=0" in out, (task, out)

    # 3. not in a worker's clone on the host
    workers = [s for s in w.store.list_sessions(job) if s.role is Role.WORKER]
    assert len(workers) == 2
    for s in workers:
        hit = subprocess.run(
            ["grep", "-rlF", MARKER, str(s.clone_path)], capture_output=True, text=True
        )
        assert hit.returncode == 1 and hit.stdout == "", (s.id, hit.stdout)
    (author,) = [s for s in w.store.list_sessions(job) if s.role is Role.HIDDEN_TEST_AUTHOR]
    assert subprocess.run(
        ["grep", "-rlF", MARKER, str(author.clone_path)], capture_output=True
    ).stdout  # the control: the author's own clone does have it

    # 4. not on any branch in the remote, in any PR, or in anything vigild stored
    log = git(w.bare, "log", "--all", "-p")
    assert MARKER not in log and "test_hidden" not in log
    assert "hidden" not in git(w.bare, "ls-tree", "-r", "--name-only", f"vigil/{job}-w1")
    assert w.publisher.prs
    assert MARKER not in json.dumps(w.publisher.prs) and "test_hidden" not in json.dumps(
        w.publisher.prs
    )
    dump = "".join(
        str(r[0])
        for table, col in (("artifacts", "content"), ("events", "data"), ("inbox_items", "body"))
        for r in w.store.q(f"SELECT {col} FROM {table}")  # noqa: S608
    )
    assert MARKER not in dump
    assert "test_hidden" not in json.dumps(
        {k: v for k, v in rec["hidden"].items() if k != "test_cmd"}
    )
