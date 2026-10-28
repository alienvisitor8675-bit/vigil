"""ADR-0004 part 5: image builds are a separate, explicit phase. A session or a
grader never triggers one; a missing image refuses with "build <image> first"."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from tests.executors.hostfakes import FakeClient
from tests.test_devimage import REPO, FakeDocker, LayerDocker  # noqa: F401
from tests.test_devimage import layered as layered
from tests.test_devimage import world as world
from vigild.config import CONFIG_DIR, Config
from vigild.devimage import StaleDevImage
from vigild.executors.hosts import HostRefused
from vigild.executors.runtime import DockerRuntime, DockerTestRunner

ROOT = CONFIG_DIR.parent
BUILD_CALLS = {"ensure_fresh", "build", "build_and_validate", "pull"}
# the explicit build steps in these files: the owner's rebuild (an inbox action) and vigild's
# own deps-merge handler (not a session or a grader)
EXPLICIT = {"on_rebuild", "_build_after_deps"}
SESSION_AND_GRADER_FILES = [
    "vigild/jobs/runner.py",
    "vigild/verifier.py",
    "vigild/executors/runtime.py",
    "vigild/executors/hosts.py",
    "vigild/jobs/deps.py",
]


def _build_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found: list[str] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node.name in EXPLICIT:
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            # `self.build(job_id)` in the runner is the job's build stage (dispatching
            # workers), not an image build; image builds go through `self.images...`
            stage = (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and (f.value.id == "self" and name == "build" and path.name == "runner.py")
            )
            if name in BUILD_CALLS and not stage:
                found.append(f"{path.name}:{node.lineno} {name}")
            self.generic_visit(node)

    V().visit(tree)
    return found


@pytest.mark.parametrize("rel", SESSION_AND_GRADER_FILES)
def test_no_session_or_grader_path_calls_a_build(rel: str) -> None:
    assert _build_calls(ROOT / rel) == []


def test_the_scan_sees_a_build_call(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def run(self):\n    self.images.ensure_fresh('a', 'b')\n    c.images.pull('i')\n")
    assert len(_build_calls(f)) == 2
    f.write_text("def on_rebuild(self):\n    self.images.ensure_fresh('a', 'b')\n")
    assert _build_calls(f) == []


def test_resolve_never_builds_and_names_the_build(world: Any) -> None:
    images, docker, _, _, base, ctx = world
    with pytest.raises(StaleDevImage, match=r"build vigil-dev/fixture:.* first"):
        images.resolve(REPO, base)
    assert docker.builds == []
    assert ctx["pages"] and ctx["pages"][0]["stage"] == "missing"  # paged once, with a rebuild
    tag = images.ensure_fresh(REPO, base)  # the explicit build step
    n = len(docker.builds)
    assert images.resolve(REPO, base) == tag and len(docker.builds) == n


def test_resolve_refuses_an_image_that_changed_since_validation(world: Any) -> None:
    images, docker, _, _, base, _ = world
    tag = images.ensure_fresh(REPO, base)
    docker.ids[tag] = "sha256:other"
    with pytest.raises(StaleDevImage, match="first"):
        images.resolve(REPO, base)


def test_resolve_keeps_a_failed_images_block(world: Any) -> None:
    images, docker, _, _, base, _ = world
    docker.ok = False
    with pytest.raises(StaleDevImage):
        images.ensure_fresh(REPO, base)
    with pytest.raises(StaleDevImage, match="failed"):
        images.resolve(REPO, base)


def test_session_resolve_needs_both_images_built(layered: Any) -> None:
    sessions, docker, _, base, _ = layered
    with pytest.raises(StaleDevImage, match="first"):
        sessions.resolve(REPO, base)
    tag = sessions.ensure_fresh(REPO, base)
    n = len(docker.args)
    assert sessions.resolve(REPO, base) == tag and len(docker.args) == n


def test_the_runtime_refuses_a_missing_image_without_a_pull(cfg: Config, tmp_path: Path) -> None:
    from vigild.executors.base import SessionSpec
    from vigild.jobs.model import Role

    client = FakeClient()
    spec = SessionSpec(
        session_id="s-1", role=Role.WORKER, job_id="j", task_id="t", repo="o/r", preset="p",
        model="m", clone=tmp_path, session_dir=tmp_path / "s", max_turns=1, max_minutes=1,
        max_budget_usd=1.0, image="vigil-session/x:1",
    )  # fmt: skip
    with pytest.raises(HostRefused, match="build vigil-session/x:1 first"):
        DockerRuntime(cfg, client).run(spec, "tok", lambda e: None)
    with pytest.raises(HostRefused, match="build vigil-dev/x:1 first"):
        DockerTestRunner(cfg, client).run(
            "vigil-dev/x:1", tmp_path, "true", timeout_s=5, max_output=10
        )
    assert client.calls == []
