"""Per-repo dev images (owner direction, review round 2): built from the
repo's lockfiles at the base commit, validated by its suites offline, keyed on
those lockfiles, and a failure blocks dispatch and pages once per key."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git
from vigild.config import Config, RepoPolicy
from vigild.db import Store
from vigild.devimage import DevImages, StaleDevImage, missing_tools, tools_in
from vigild.verifier import RunResult
from vigild.workspace import Workspace

REPO = "o/fixture"
PA = "thomasbardhi01/parkagent"


class FakeDocker:
    """Builds succeed (or not) and get a fresh image id each time."""

    def __init__(self) -> None:
        self.ok = True
        self.ids: dict[str, str] = {}
        self.builds: list[tuple[str, list[str]]] = []

    def image_id_of(self, image: str) -> str | None:
        return self.ids.get(image)

    def build(
        self,
        context: Path,
        dockerfile: Path,
        tag: str,
        *,
        timeout_s: int,
        build_args: dict[str, str] | None = None,
        contexts: dict[str, Path] | None = None,
        labels: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        files = sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())
        self.builds.append((tag, files))
        if not self.ok:
            return False, "E: Unable to locate package"
        self.ids[tag] = f"sha256:{len(self.builds):04d}"
        return True, "built"

    def run_check(self, image: str, command: str) -> tuple[int, str]:
        return 0, "claude 2.1.306"


class FakeSuites:
    def __init__(self) -> None:
        self.exit: dict[str, int] = {}
        self.ran: list[tuple[str, str, list[str]]] = []

    def run(
        self, image: str, source: Path, command: str, *, timeout_s: int, max_output: int
    ) -> RunResult:
        files = sorted(p.name for p in source.iterdir())
        self.ran.append((image, command, files))
        return RunResult(self.exit.get(command, 0), f"ran {command}")


@pytest.fixture
def world(
    cfg: Config, store: Store, tmp_path: Path, fixture_repo: dict[str, Any]
) -> tuple[DevImages, FakeDocker, FakeSuites, Workspace, str, dict[str, Any]]:
    seed = fixture_repo["seed"]
    (seed / "uv.lock").write_text("version = 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "lock")
    git(seed, "push", "-q", "origin", "main")
    cfg.repos.repos[REPO] = RepoPolicy(
        decider=["haiku"],
        backends=["claude-haiku"],
        dev_dockerfile="images/dev/vigil.Dockerfile",
        dev_lockfiles=["uv.lock", "pyproject.toml"],
        dev_suites={"unit": "grep -q fixture README.md", "docs": "test -f docs/runbook.md"},
        dev_excluded={"ios": "needs Xcode"},
        dev_image_tools=["grep", "test"],
    )
    ws = Workspace(tmp_path / "data", remote_url=lambda r: str(fixture_repo["bare"]))
    docker, suites = FakeDocker(), FakeSuites()
    pages: list[dict[str, Any]] = []
    images = DevImages(
        cfg, store, docker, ws, suites, on_failure=lambda r, t, b: pages.append({"title": t, **b})
    )
    base = ws.head_sha(REPO, "main")
    return images, docker, suites, ws, base, {"pages": pages, "seed": seed}


def test_every_repo_names_a_dev_image_definition(cfg: Config) -> None:
    for repo, pol in cfg.repos.repos.items():
        assert pol.dev_dockerfile.startswith("images/dev/"), repo
        assert pol.dev_lockfiles and pol.dev_suites, repo


def test_parkagent_records_what_it_cant_run(cfg: Config) -> None:
    pol = cfg.repo(PA)
    assert "ios" in pol.dev_excluded and "Xcode" in pol.dev_excluded["ios"]
    assert {"server", "server-pg", "executor", "scripts", "data", "boot", "build"} <= set(
        pol.dev_suites
    )


def test_a_new_key_is_built_from_lockfiles_only_and_validated(world: Any) -> None:
    images, docker, suites, _, base, _ = world
    tag = images.ensure_fresh(REPO, base)
    assert tag.startswith("vigil-dev/fixture:")
    ((built, files),) = docker.builds
    assert built == tag and files == ["uv.lock"]  # lockfiles only: no source, no missing file
    assert [c for _, c, _ in suites.ran] == list(images.cfg.repo(REPO).dev_suites.values())
    assert all("README.md" in files for _, _, files in suites.ran)  # a clean checkout
    row = images.latest(REPO)
    assert row["ok"] and row["stage"] == "validate" and row["base_sha"] == base
    assert json.loads(row["output"])["excluded"] == {"ios": "needs Xcode"}


def test_the_same_lockfiles_reuse_the_image(world: Any) -> None:
    images, docker, _, ws, base, ctx = world
    tag = images.ensure_fresh(REPO, base)
    seed = ctx["seed"]
    (seed / "README.md").write_text("# fixture, edited\n")  # not a lockfile
    git(seed, "commit", "-qam", "docs")
    git(seed, "push", "-q", "origin", "main")
    assert images.ensure_fresh(REPO, ws.head_sha(REPO, "main")) == tag
    assert len(docker.builds) == 1


def test_a_lockfile_change_builds_a_new_image(world: Any) -> None:
    images, docker, _, ws, base, ctx = world
    first = images.ensure_fresh(REPO, base)
    seed = ctx["seed"]
    (seed / "uv.lock").write_text("version = 2\n")
    git(seed, "commit", "-qam", "bump")
    git(seed, "push", "-q", "origin", "main")
    second = images.ensure_fresh(REPO, ws.head_sha(REPO, "main"))
    assert second != first and len(docker.builds) == 2


def test_a_failing_suite_blocks_and_pages_once(world: Any) -> None:
    images, docker, suites, _, base, ctx = world
    suites.exit["test -f docs/runbook.md"] = 1
    with pytest.raises(StaleDevImage, match="validate"):
        images.ensure_fresh(REPO, base)
    with pytest.raises(StaleDevImage, match="rebuild it from the inbox"):
        images.ensure_fresh(REPO, base)  # not rebuilt on every dispatch
    assert len(docker.builds) == 1
    (page,) = ctx["pages"]
    assert page["kind"] == "dev_image" and page["failed_suites"] == ["docs"]
    assert page["actions"] == ["rebuild", "dismiss"] and page["base_sha"] == base
    suites.exit.clear()
    assert images.ensure_fresh(REPO, base, force=True)  # the owner's rebuild
    assert len(docker.builds) == 2


def test_a_failed_build_blocks_and_pages(world: Any) -> None:
    images, docker, suites, _, base, ctx = world
    docker.ok = False
    with pytest.raises(StaleDevImage, match="build"):
        images.ensure_fresh(REPO, base)
    assert suites.ran == []
    assert ctx["pages"][0]["stage"] == "build"
    assert "Unable to locate" in json.dumps(ctx["pages"][0]["detail"])


def test_an_image_changed_since_validation_is_rebuilt(world: Any) -> None:
    images, docker, _, _, base, _ = world
    tag = images.ensure_fresh(REPO, base)
    docker.ids[tag] = "sha256:tampered"
    assert images.status(REPO, base) == (False, f"{tag} changed since it was validated")
    images.ensure_fresh(REPO, base)
    assert len(docker.builds) == 2
    assert images.status(REPO, base) == (True, tag)


# --- blocker 4: a test the dev image can't run is refused, not accepted -------------------


@pytest.mark.parametrize(
    "cmd,tools",
    [
        ("pnpm test", ["pnpm"]),
        ("grep -q x a.md && test -f b", ["grep", "test"]),
        ("CI=1 pnpm -C server test | tail -3", ["pnpm", "tail"]),
        ("cd docs; grep x a.md || true", ["cd", "grep", "true"]),
    ],
)
def test_tools_in(cmd: str, tools: list[str]) -> None:
    assert tools_in(cmd) == tools


def test_parkagent_runs_its_real_tests_but_not_xcode(cfg: Config) -> None:
    assert missing_tools(cfg, PA, "pnpm -C server test") == []
    assert missing_tools(cfg, PA, "xcodebuild test") == ["xcodebuild"]


# --- owner direction, round 3: session images = the executor layer over the dev image --------


class LayerDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.args: list[dict[str, Any]] = []
        self.checks: list[tuple[str, str]] = []
        self.check_exit = 0

    def build(  # type: ignore[override]
        self,
        context: Path,
        dockerfile: Path,
        tag: str,
        *,
        timeout_s: int,
        build_args: dict[str, str] | None = None,
        contexts: dict[str, Path] | None = None,
        labels: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        self.args.append({"tag": tag, "dockerfile": dockerfile, "build_args": build_args or {},
                          "contexts": sorted(contexts or {}), "labels": labels or {}})  # fmt: skip
        if "claude-executor" in str(dockerfile):
            self.ids[tag] = f"sha256:layer{len(self.args):04d}"
            return True, "built"
        return super().build(context, dockerfile, tag, timeout_s=timeout_s)

    def run_check(self, image: str, command: str) -> tuple[int, str]:
        self.checks.append((image, command))
        return self.check_exit, "claude 2.1.306"


@pytest.fixture
def layered(world: Any, tmp_path: Path) -> tuple[Any, LayerDocker, DevImages, str, dict[str, Any]]:
    from vigild.devimage import SessionImages

    images, _, _, _, base, ctx = world
    docker = LayerDocker()
    images.docker = docker
    mods = tmp_path / "mods"
    mods.mkdir()
    sessions = SessionImages(images, lambda: mods, on_failure=images.on_failure)
    return sessions, docker, images, base, ctx


def test_a_session_image_is_the_executor_layer_over_the_dev_image(layered: Any) -> None:
    sessions, docker, images, base, _ = layered
    tag = sessions.ensure_fresh(REPO, base)
    dev_tag = images.ensure_fresh(REPO, base)
    assert tag.startswith("vigil-session/fixture:") and dev_tag.startswith("vigil-dev/fixture:")
    _, layer_build = docker.args
    assert layer_build["dockerfile"].name == "Dockerfile"
    assert layer_build["dockerfile"].parent.name == "claude-executor"  # one layer definition
    assert layer_build["build_args"] == {"BASE": dev_tag}
    assert layer_build["contexts"] == ["claude_mods", "vigil_configs", "vigil_schemas"]
    # the image says which layer built it (the threat-row fixture picks by it)
    from vigild.devimage import LAYER_LABEL, layer_digest

    assert layer_build["labels"][LAYER_LABEL] == layer_digest()
    assert docker.checks and "2.1.306" in docker.checks[0][1]
    assert sessions.ensure_fresh(REPO, base) == tag and len(docker.args) == 2  # reused


def test_a_new_dev_image_or_layer_change_rebuilds_the_session_image(
    layered: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigild import devimage

    sessions, docker, _, base, _ = layered
    first = sessions.ensure_fresh(REPO, base)
    monkeypatch.setattr(devimage, "layer_digest", lambda: "changed")
    second = sessions.ensure_fresh(REPO, base)
    assert second != first and len(docker.args) == 3  # the layer only; the dev image stands


def test_a_failed_session_image_blocks_and_pages(layered: Any) -> None:
    sessions, docker, _, base, ctx = layered
    docker.check_exit = 1
    with pytest.raises(StaleDevImage, match="session"):
        sessions.ensure_fresh(REPO, base)
    page = ctx["pages"][-1]
    assert page["kind"] == "dev_image" and page["stage"] == "session-validate"
    assert page["actions"] == ["rebuild", "dismiss"]


def test_a_build_finds_dockers_credential_helper_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2a, item 0: vigild may run with a PATH (launchd, a bare shell)
    that lacks Docker Desktop's bin, where `docker-credential-desktop` lives
    beside the real CLI. The build must still find it."""
    import os

    from vigild.devimage import DockerImages

    real = tmp_path / "Docker.app" / "Resources" / "bin"
    real.mkdir(parents=True)
    helper = real / "docker-credential-desktop"
    helper.write_text("#!/bin/sh\necho '{}'\n")
    cli = real / "docker"
    cli.write_text(
        "#!/bin/sh\n"
        "command -v docker-credential-desktop >/dev/null || {\n"
        "  echo 'docker-credential-desktop: executable file not found in $PATH' >&2; exit 1; }\n"
        "echo built\n"
    )
    for f in (helper, cli):
        f.chmod(0o755)
    link = tmp_path / "home" / ".docker" / "bin" / "docker"  # the CLI's usual symlink
    link.parent.mkdir(parents=True)
    os.symlink(cli, link)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    ok, out = DockerImages(client=None, cli=str(link)).build(
        ctx, ctx / "Dockerfile", "t:1", timeout_s=30
    )
    assert ok, out
    assert "built" in out
