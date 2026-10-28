"""Per-repo dev images, built and validated by vigild (owner direction,
review round 2). This replaces the nightly validation of a hand-built image.

- **Definition:** each repo in configs/repos.yaml names `dev_dockerfile`
  (images/dev/<repo>.Dockerfile), the `dev_lockfiles` its build installs from,
  the `dev_suites` that prove it, and `dev_excluded`: suites that can't run in
  a Linux container, each with its reason.
- **Key:** sha256 over the Dockerfile, each lockfile's blob id at the base
  commit (from vigild's mirror), and the suites. The image is
  `vigil-dev/<name>:<key[:16]>`. A new key is a new image.
- **Build:** the context is only the lockfiles, copied from the base commit;
  no repo source goes into the image. The build has the network; nothing after
  it does.
- **Validate:** every suite runs on a clean checkout of the base commit, inside
  the image, through the verifier's runner: no network, the agent uid, output
  capped. The image is fresh only if every suite passes.
- **Fresh:** `ensure_fresh(repo, base_sha)` builds and validates a key it hasn't
  seen. A failed build or validation, or an image that's gone or changed since
  it was validated, raises StaleDevImage, which blocks dispatch for the repo,
  and pages the owner once per key with `rebuild` or `dismiss`. A failed key
  is not rebuilt until the owner asks (`force=True`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from vigild.config import CONFIG_DIR, Config
from vigild.db import Store, iso, utcnow
from vigild.shellwords import programs
from vigild.verifier import RunResult, TestRunner, outcome

ROOT = CONFIG_DIR.parent
BUILD_SECONDS = 60 * 60
SUITE_SECONDS = 30 * 60
SUITE_OUTPUT = 256 << 10
TAIL = 3000


class StaleDevImage(RuntimeError):
    pass


class MissingImage(StaleDevImage):
    """The image isn't built (or changed since it was validated). Builds are an
    explicit phase (ADR-0004 §5): a session or grader never starts one."""


SHELL_BUILTINS = {"cd", "true", "false", "test", "[", "echo", "exit", "export", "set"}


def tools_in(test_cmd: str) -> list[str]:
    """The program each command in a shell line runs (the one parser, F8)."""
    return programs(test_cmd)


def missing_tools(cfg: Config, repo: str, test_cmd: str | None) -> list[str]:
    """Tools a test_cmd needs that the repo's dev image doesn't declare."""
    have = set(cfg.repo(repo).dev_image_tools) | SHELL_BUILTINS
    return [t for t in tools_in(test_cmd or "") if t not in have]


class DockerLike(Protocol):
    def image_id_of(self, image: str) -> str | None: ...
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
    ) -> tuple[bool, str]: ...
    def run_check(self, image: str, command: str) -> tuple[int, str]: ...


class Sources(Protocol):
    """What DevImages needs from the Workspace: blob ids and files at a commit
    in vigild's mirror, and a clean checkout of it."""

    def blob_ids(
        self, repo: str, sha: str, paths: list[str], source: str = "mirror"
    ) -> dict[str, str | None]: ...
    def export_files(
        self, repo: str, sha: str, paths: list[str], dest: Path, source: str = "mirror"
    ) -> None: ...
    def verify_checkout(
        self,
        repo: str,
        name: str,
        sha: str,
        *,
        take: list[tuple[str, list[str]]] | None = None,
        source: str = "vault",
    ) -> Path: ...


@dataclass(frozen=True)
class Validation:
    repo: str
    key: str
    image: str
    image_id: str | None
    ok: bool
    stage: str  # build | validate
    output: str  # JSON: the build tail, or each suite's outcome and tail
    base_sha: str
    validated_at: str


OnFailure = Callable[[str, str, dict[str, Any]], None]


class DevImages:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        docker: DockerLike,
        sources: Sources,
        runner: TestRunner,
        *,
        on_failure: OnFailure | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.docker = docker
        self.sources = sources
        self.runner = runner
        self.on_failure = on_failure
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # --- identity -------------------------------------------------------------------

    def dockerfile(self, repo: str) -> Path:
        return ROOT / self.cfg.repo(repo).dev_dockerfile

    def key(self, repo: str, base_sha: str, source: str = "mirror") -> str:
        pol = self.cfg.repo(repo)
        blobs = self.sources.blob_ids(repo, base_sha, list(pol.dev_lockfiles), source)
        h = hashlib.sha256()
        h.update(self.dockerfile(repo).read_bytes())
        for path in sorted(blobs):
            h.update(f"\0{path}={blobs[path] or '-'}".encode())
        h.update(b"\0" + json.dumps(pol.dev_suites, sort_keys=True).encode())
        return h.hexdigest()

    def tag(self, repo: str, key: str) -> str:
        name = re.sub(r"[^a-z0-9._-]", "-", repo.split("/")[-1].lower())
        return f"vigil-dev/{name}:{key[:16]}"

    def latest(self, repo: str, key: str | None = None) -> dict[str, Any] | None:
        sql, args = "SELECT * FROM dev_images WHERE repo=?", [repo]
        if key is not None:
            sql += " AND key=?"
            args.append(key)
        rows = self.store.q(sql + " ORDER BY id DESC LIMIT 1", tuple(args))
        return dict(rows[0]) if rows else None

    # --- freshness ------------------------------------------------------------------

    def status(self, repo: str, base_sha: str) -> tuple[bool, str]:
        """Is the image for this base built, validated and unchanged? No side effects."""
        key = self.key(repo, base_sha)
        tag = self.tag(repo, key)
        last = self.latest(repo, key)
        if last is None:
            return False, f"{tag} not built yet (lockfiles changed or first run)"
        if not last["ok"]:
            return False, f"{tag} failed its {last['stage']} at {last['validated_at']}"
        current = self.docker.image_id_of(tag)
        if current is None:
            return False, f"{tag} is not present"
        if current != last["image_id"]:
            return False, f"{tag} changed since it was validated"
        return True, tag

    def ensure_fresh(
        self, repo: str, base_sha: str, *, force: bool = False, source: str = "mirror"
    ) -> str:
        """The validated image for this base, built now if its key is new.
        `source="vault"`: a commit only vigild's vault has (a deps job's head,
        plan 2a item 5): built from its lockfiles, its suites run on it."""
        with self._lock(repo):
            key = self.key(repo, base_sha, source)
            tag = self.tag(repo, key)
            last = self.latest(repo, key)
            if last is not None and not force:
                if not last["ok"]:
                    raise StaleDevImage(
                        f"{repo}: dev image {tag} failed its {last['stage']}; "
                        "rebuild it from the inbox"
                    )
                current = self.docker.image_id_of(tag)
                if current is not None and current == last["image_id"]:
                    return tag
            v = self.build_and_validate(repo, base_sha, key, source)
            if not v.ok:
                self._page(v)
                raise StaleDevImage(f"{repo}: dev image {tag} failed its {v.stage}")
            return tag

    def resolve(self, repo: str, base_sha: str, *, source: str = "mirror") -> str:
        """The validated image for this base, or a refusal. Never builds: a
        session or a grader calls this, and the build is the explicit step
        (`ensure_fresh`: `vigil dev-image`, or the owner's rebuild)."""
        key = self.key(repo, base_sha, source)
        tag = self.tag(repo, key)
        last = self.latest(repo, key)
        if last is not None and not last["ok"]:
            raise StaleDevImage(
                f"{repo}: dev image {tag} failed its {last['stage']}; rebuild it from the inbox"
            )
        if last is None or self.docker.image_id_of(tag) != last["image_id"]:
            self._missing(repo, key, tag, base_sha, "dev image")
        return tag

    def _missing(self, repo: str, key: str, tag: str, base_sha: str, what: str) -> None:
        if self.on_failure is not None:
            self.on_failure(
                repo,
                f"{repo}: {what} {tag} is not built",
                {
                    "kind": "dev_image",
                    "repo": repo,
                    "key": key,
                    "image": tag,
                    "stage": "missing",
                    "base_sha": base_sha,
                    "failed_suites": [],
                    "detail": {"build": f"build {tag} first"},
                    "actions": ["rebuild", "dismiss"],
                },
            )
        raise MissingImage(f"build {tag} first")

    def build_and_validate(
        self, repo: str, base_sha: str, key: str, source: str = "mirror"
    ) -> Validation:
        pol = self.cfg.repo(repo)
        tag = self.tag(repo, key)
        ctx = Path(tempfile.mkdtemp(prefix="vigil-devctx-"))
        try:
            self.sources.export_files(repo, base_sha, list(pol.dev_lockfiles), ctx, source)
            ok, out = self.docker.build(ctx, self.dockerfile(repo), tag, timeout_s=BUILD_SECONDS)
        finally:
            shutil.rmtree(ctx, ignore_errors=True)
        image_id = self.docker.image_id_of(tag) if ok else None
        if not ok or image_id is None:
            failed: dict[str, Any] = {"build": out[-TAIL:]}
            return self._record(repo, key, tag, None, False, "build", failed, base_sha)
        src = self.sources.verify_checkout(repo, f"devimage-{key[:16]}", base_sha, source=source)
        suites: dict[str, dict[str, Any]] = {}
        try:
            for name, cmd in pol.dev_suites.items():
                r: RunResult = self.runner.run(
                    tag, src, cmd, timeout_s=SUITE_SECONDS, max_output=SUITE_OUTPUT
                )
                suites[name] = {
                    "exit": r.exit,
                    "outcome": outcome(r.exit, r.timed_out),
                    "tail": r.output[-TAIL:],
                }
        finally:
            shutil.rmtree(src.parent, ignore_errors=True)
        passed = all(s["outcome"] == "pass" for s in suites.values())
        result: dict[str, Any] = {"suites": suites, "excluded": dict(pol.dev_excluded)}
        return self._record(repo, key, tag, image_id, passed, "validate", result, base_sha)

    # --- helpers --------------------------------------------------------------------

    def _lock(self, repo: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(repo, threading.Lock())

    def _record(
        self,
        repo: str,
        key: str,
        tag: str,
        image_id: str | None,
        ok: bool,
        stage: str,
        body: dict[str, Any],
        base_sha: str,
    ) -> Validation:
        v = Validation(
            repo, key, tag, image_id, ok, stage, json.dumps(body), base_sha, iso(self.clock())
        )
        self.store.x(
            "INSERT INTO dev_images (repo, image, image_id, ok, output, validated_at, key, stage,"
            " base_sha) VALUES (?,?,?,?,?,?,?,?,?)",
            (repo, tag, image_id, int(ok), v.output, v.validated_at, key, stage, base_sha),
        )
        return v

    def _page(self, v: Validation) -> None:
        if self.on_failure is None:
            return
        body = json.loads(v.output)
        failed = [n for n, s in (body.get("suites") or {}).items() if s.get("outcome") != "pass"]
        suffix = f" ({', '.join(failed)})" if failed else ""
        self.on_failure(
            v.repo,
            f"{v.repo}: dev image {v.stage} failed{suffix}",
            {
                "kind": "dev_image",
                "repo": v.repo,
                "key": v.key,
                "image": v.image,
                "stage": v.stage,
                "base_sha": v.base_sha,
                "failed_suites": failed,
                "detail": body,
                "actions": ["rebuild", "dismiss"],
            },
        )


EXECUTOR_LAYER = ROOT / "images" / "claude-executor"
LAYER_LABEL = "dev.vigil.layer-digest"
SESSION_KEY_LABEL = "dev.vigil.session-key"
MANAGED_SETTINGS = ROOT / "configs" / "managed-settings.json"
REPORT_SCHEMA = ROOT / "schemas" / "worker_report.schema.json"  # the Stop hook's (item 6)
# What a session image must have: the CLI at its pin, the harness's Python
# with the SDK, setpriv for the agent uid, and guard seeded.
SESSION_CHECK = (
    "claude --version | grep -q '^2\\.1\\.306' && echo claude 2.1.306"
    " && /opt/vigil-venv/bin/python -c 'import claude_agent_sdk'"
    " && command -v setpriv >/dev/null && test -d /opt/claude-seed"
    " && test -x /opt/vigil/agent-cli && id -u 10001 >/dev/null"
)


def layer_digest() -> str:
    """The executor layer's identity: every file it's built from."""
    h = hashlib.sha256()
    files = sorted(
        f for f in EXECUTOR_LAYER.rglob("*") if f.is_file() and "__pycache__" not in f.parts
    )
    for f in [*files, MANAGED_SETTINGS, REPORT_SCHEMA]:
        h.update(str(f.relative_to(ROOT)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


class SessionImages:
    """Each repo's session image (owner direction, round 3): the one executor
    layer (images/claude-executor/Dockerfile) built over that repo's dev
    image, so workers have its toolchain and dependencies offline. Keyed on
    the dev image and the layer (and guard's pinned SHA); built, checked,
    blocked and paged like a dev image. The verifier never uses it."""

    def __init__(
        self,
        dev: DevImages,
        claude_mods: Callable[[], Path],
        *,
        on_failure: OnFailure | None = None,
    ) -> None:
        self.dev = dev
        self.claude_mods = claude_mods  # a checkout of claude-mods at the pinned SHA
        self.on_failure = on_failure

    def key(self, dev_tag: str) -> str:
        sha = self.dev.cfg.vigild.github.claude_mods_sha
        return hashlib.sha256(f"{dev_tag}\0{layer_digest()}\0{sha}".encode()).hexdigest()

    def tag(self, repo: str, key: str) -> str:
        return self.dev.tag(repo, key).replace("vigil-dev/", "vigil-session/", 1)

    def resolve(self, repo: str, base_sha: str) -> str:
        """The built session image, or a refusal. Never builds (ADR-0004 §5)."""
        dev_tag = self.dev.resolve(repo, base_sha)
        key = self.key(dev_tag)
        tag = self.tag(repo, key)
        last = self.dev.latest(repo, key)
        if last is not None and not last["ok"]:
            raise StaleDevImage(
                f"{repo}: session image {tag} failed its {last['stage']}; rebuild it from the inbox"
            )
        if last is None or self.dev.docker.image_id_of(tag) != last["image_id"]:
            self.dev._missing(repo, key, tag, base_sha, "session image")
        return tag

    def ensure_fresh(self, repo: str, base_sha: str, *, force: bool = False) -> str:
        dev_tag = self.dev.ensure_fresh(repo, base_sha, force=force)
        with self.dev._lock(f"session:{repo}"):
            key = self.key(dev_tag)
            tag = self.tag(repo, key)
            last = self.dev.latest(repo, key)
            if last is not None and not force:
                if not last["ok"]:
                    raise StaleDevImage(
                        f"{repo}: session image {tag} failed its {last['stage']}; "
                        "rebuild it from the inbox"
                    )
                current = self.dev.docker.image_id_of(tag)
                if current is not None and current == last["image_id"]:
                    return tag
            v = self._build(repo, key, tag, dev_tag, base_sha)
            if not v.ok:
                if self.on_failure is not None:
                    self.on_failure(
                        repo,
                        f"{repo}: session image {v.stage.removeprefix('session-')} failed",
                        {
                            "kind": "dev_image",
                            "repo": repo,
                            "key": key,
                            "image": tag,
                            "stage": v.stage,
                            "base_sha": base_sha,
                            "failed_suites": [],
                            "detail": json.loads(v.output),
                            "actions": ["rebuild", "dismiss"],
                        },
                    )
                raise StaleDevImage(f"{repo}: session image {tag} failed its {v.stage}")
            return tag

    def _build(self, repo: str, key: str, tag: str, dev_tag: str, base_sha: str) -> Validation:
        ok, out = self.dev.docker.build(
            EXECUTOR_LAYER,
            EXECUTOR_LAYER / "Dockerfile",
            tag,
            timeout_s=BUILD_SECONDS,
            build_args={"BASE": dev_tag},
            contexts={
                "claude_mods": self.claude_mods(),
                "vigil_configs": ROOT / "configs",
                "vigil_schemas": ROOT / "schemas",
            },
            labels={LAYER_LABEL: layer_digest(), SESSION_KEY_LABEL: key},
        )
        image_id = self.dev.docker.image_id_of(tag) if ok else None
        if not ok or image_id is None:
            fail: dict[str, Any] = {"build": out[-TAIL:]}
            return self.dev._record(repo, key, tag, None, False, "session-build", fail, base_sha)
        code, check = self.dev.docker.run_check(tag, SESSION_CHECK)
        body: dict[str, Any] = {"check": check[-TAIL:], "base": dev_tag}
        return self.dev._record(
            repo, key, tag, image_id, code == 0, "session-validate", body, base_sha
        )


def docker_cli(cli: str | None = None) -> tuple[str, dict[str, str]]:
    """The docker CLI and an environment it can run in (phase 2a, item 0).

    Docker Desktop's CLI calls `docker-credential-desktop`, which lives beside
    the real binary (`~/.docker/bin/docker` is a symlink to it). vigild under
    launchd, or any bare shell, may not have that directory on PATH; the CLI
    then fails with "executable file not found". So the CLI's own directory,
    and the directory it resolves to, go first on PATH."""
    path = cli or shutil.which("docker") or str(Path.home() / ".docker/bin/docker")
    dirs = [str(Path(path).parent), str(Path(path).resolve().parent)]
    rest = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    env = {**os.environ, "PATH": os.pathsep.join(dict.fromkeys([*dirs, *rest]))}
    return path, env


class DockerImages:
    """The real DockerLike. Builds go through the docker CLI, so they get
    BuildKit (the dev Dockerfiles use heredocs and `COPY --chmod`); they have
    the network, since they install from the lockfiles. Nothing that runs in
    the image does."""

    def __init__(self, client: Any, cli: str | None = None) -> None:
        self.client = client
        self.cli, self.env = docker_cli(cli)

    def image_id_of(self, image: str) -> str | None:
        import docker.errors

        try:
            return str(self.client.images.get(image).id)
        except docker.errors.ImageNotFound:
            return None

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
        cmd = [self.cli, "build", "--progress=plain", "-f", str(dockerfile), "-t", tag,
               "--label", f"dev.vigil.devimage={tag}"]  # fmt: skip
        for k, v in (build_args or {}).items():
            cmd += ["--build-arg", f"{k}={v}"]
        for k, path in (contexts or {}).items():
            cmd += ["--build-context", f"{k}={path}"]
        for k, v in (labels or {}).items():
            cmd += ["--label", f"{k}={v}"]
        cmd.append(str(context))
        try:
            p = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=self.env,
            )
        except subprocess.TimeoutExpired:
            return False, f"the build took longer than {timeout_s} s"
        except OSError as e:
            return False, f"docker CLI: {e}"
        return p.returncode == 0, (p.stdout + p.stderr)[-20000:]

    def run_check(self, image: str, command: str) -> tuple[int, str]:
        """A command in the image, as the agent uid, with no network."""
        c = self.client.containers.run(
            image,
            entrypoint=["sh", "-c"],
            command=[command],
            user="10001:10001",
            detach=True,
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,mode=1777"},  # noqa: S108
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
        try:
            code = int(c.wait(timeout=300).get("StatusCode", -1))
            return code, c.logs().decode(errors="replace")
        finally:
            c.remove(force=True)
