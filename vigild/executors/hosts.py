"""Sandbox hosts (ADR-0004): which host runs a session or a grader, and the
Docker client for it.

A host is only what configs/sandbox_hosts.yaml says. Selection reads the
image's architecture from its manifest and the use (`jobs` or `bench`); normal
jobs go to `local`, a bench task to an amd64 host that lists `bench`. With no
such host a bench task runs on `local`, labelled `emulated`, if the config
allows it, and is otherwise refused with a reason. The grader gets its client
from the same placement as the session, so both run on one host.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from vigild.config import Config, SandboxHost

Use = Literal["jobs", "bench"]
# Copying a worker's files to a remote host (and its results back) over the Docker
# API is not built yet, so no session and no grader is placed on a remote host.
FILE_TRANSFER_BUILT = False
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class HostRefused(RuntimeError):
    """No host may run this; the message says why."""


@dataclass(frozen=True)
class Placement:
    host: SandboxHost
    emulated: bool = False

    @property
    def label(self) -> str:
        return "emulated" if self.emulated else "native"


def normalize_arch(raw: str) -> str:
    try:
        return ARCHES[raw.lower()]
    except KeyError as e:
        raise HostRefused(f"unsupported architecture {raw!r}") from e


def pick_host(cfg: Config, *, use: Use, image_arch: str) -> Placement:
    arch = normalize_arch(image_arch)
    sh = cfg.sandbox_hosts
    if use == "jobs":
        # normal jobs run on `local` today (ADR-0004 §2): their images are built there
        local = sh.local
        if "jobs" in local.uses and local.arch == arch:
            return Placement(local)
        raise HostRefused(f"no sandbox host takes normal jobs for {arch} images")
    remote_first = sorted(sh.hosts, key=lambda h: not h.remote)
    for h in remote_first:
        if "bench" in h.uses and h.arch == arch:
            return Placement(h)
    local = sh.local
    if "bench" in local.uses and arch != local.arch and sh.bench.allow_emulation:
        return Placement(local, emulated=True)
    if arch != local.arch:
        raise HostRefused(
            f"no {arch} sandbox host lists bench, and bench.allow_emulation is off "
            "(configs/sandbox_hosts.yaml)"
        )
    raise HostRefused(f"no sandbox host lists bench for {arch} images")


def image_arch(client: Any, image: str) -> str:
    """The image's architecture, from its manifest. A missing image is a missing
    build, never a pull."""
    import docker.errors

    try:
        return normalize_arch(str(client.images.get(image).attrs["Architecture"]))
    except docker.errors.ImageNotFound as e:
        raise HostRefused(f"build {image} first") from e


def endpoint_of(context: str) -> str:
    """The Docker endpoint of a named context (docker-py reads the CLI's store)."""
    from docker.context import ContextAPI

    ctx = ContextAPI.get_context(context)
    if ctx is None:
        raise HostRefused(f"no Docker context named {context!r}")
    return str(ctx.Host)


def client_for(
    host: SandboxHost,
    *,
    lookup: Callable[[str], str] = endpoint_of,
    make: Callable[..., Any] | None = None,
    from_env: Callable[[], Any] | None = None,
) -> Any:
    """The Docker client for a host. `local` is the default Docker; a remote
    host's comes from its context, and only an `ssh://` endpoint is accepted."""
    import docker

    if not host.remote:
        return (from_env or docker.from_env)()
    endpoint = lookup(host.docker_context)
    if not endpoint.startswith("ssh://"):
        raise HostRefused(f"{host.name}: the Docker context's endpoint must be ssh://, not tcp://")
    return (make or docker.DockerClient)(base_url=endpoint, use_ssh_client=True)


class Hosts:
    """One Docker client, one session runtime and one grader per host."""

    def __init__(
        self, cfg: Config, *, make_client: Callable[[SandboxHost], Any] = client_for
    ) -> None:
        self.cfg = cfg
        self._make = make_client
        self._lock = threading.Lock()
        self._clients: dict[str, Any] = {}
        self._runtimes: dict[str, Any] = {}
        self._runners: dict[str, Any] = {}
        # which session containers pruning may remove (vigild's records); none until set
        self.prune_ok: Callable[[str], bool] | None = None

    def client(self, host: SandboxHost) -> Any:
        with self._lock:
            if host.name not in self._clients:
                self._clients[host.name] = self._make(host)
            return self._clients[host.name]

    def place(self, use: Use, image: str) -> Placement:
        """Where `image` runs: its arch from the manifest on the local Docker
        (where every image is built), then `pick_host`."""
        arch = image_arch(self.client(self.cfg.sandbox_hosts.local), image)
        p = pick_host(self.cfg, use=use, image_arch=arch)
        if p.host.remote and not FILE_TRANSFER_BUILT:
            raise HostRefused(
                f"{p.host.name}: a remote host can't run a session or a grader yet, because "
                "copying a worker's files to it over the Docker API is not built"
            )
        return p

    def runtime(self, placement: Placement) -> Any:
        from vigild.executors.runtime import DockerRuntime

        h = placement.host
        with self._lock:
            have = self._runtimes.get(h.name)
        if have is None:
            have = DockerRuntime(self.cfg, self.client(h), host=h, prune_ok=self.prune_ok)
            with self._lock:
                have = self._runtimes.setdefault(h.name, have)
        return have

    def test_runner(self, placement: Placement) -> Any:
        """The grader on the session's host."""
        from vigild.executors.runtime import DockerTestRunner

        h = placement.host
        with self._lock:
            have = self._runners.get(h.name)
        if have is None:
            have = DockerTestRunner(self.cfg, self.client(h), host=h)
            with self._lock:
                have = self._runners.setdefault(h.name, have)
        return have

    def bench(self, image: str) -> tuple[Placement, Any, Any]:
        """The bench entry point (slice 1b has no caller yet): where a bench
        task's image runs, with the session runtime and the grader for that
        host. The placement says `emulated` when it falls back to `local`."""
        p = self.place("bench", image)
        return p, self.runtime(p), self.test_runner(p)

    def close(self) -> None:
        for c in self._clients.values():
            with contextlib.suppress(Exception):
                c.close()

    def close_remote(self) -> None:
        for name, c in list(self._clients.items()):
            if self.cfg.sandbox_hosts.host(name).remote:
                with contextlib.suppress(Exception):
                    c.close()


class HostExecutor:
    """The runner's session executor: each session runs on the host chosen for
    its image (`Hosts.place`), so a normal job lands on `local` and a bench
    task on its bench host. A remote placement is refused by that host's
    runtime until copying a worker's files to it is built."""

    def __init__(self, hosts: Hosts, use: Use = "jobs") -> None:
        self.hosts = hosts
        self.use: Use = use
        self._where: dict[str, Placement] = {}
        self._lock = threading.Lock()

    def _image(self, spec: Any) -> str:
        return str(spec.image or self.hosts.cfg.vigild.images.claude_executor)

    def run(self, spec: Any, gateway_token: str, on_event: Callable[..., None]) -> Any:
        p = self.hosts.place(self.use, self._image(spec))
        with self._lock:
            self._where[spec.session_id] = p
        try:
            return self.hosts.runtime(p).run(spec, gateway_token, on_event)
        finally:
            with self._lock:
                self._where.pop(spec.session_id, None)

    def stop(self, session_id: str) -> None:
        with self._lock:
            p = self._where.get(session_id)
        if p is not None:
            self.hosts.runtime(p).stop(session_id)

    def run_deps(
        self, image: str, work: Any, command: str, timeout_s: int = 900
    ) -> tuple[int, str]:
        p = self.hosts.place(self.use, image)
        return self.hosts.runtime(p).run_deps(image, work, command, timeout_s)  # type: ignore[no-any-return]

    def reap(self) -> int:
        """At startup: the local host's leftovers. A remote host is only
        reached when a session is placed on it."""
        local = Placement(self.hosts.cfg.sandbox_hosts.local)
        return int(self.hosts.runtime(local).reap())


class HostTestRunner:
    """The verifier's test runner: a grader runs on the same host the session's
    image is placed on, so both are on one host."""

    def __init__(self, hosts: Hosts, use: Use = "jobs") -> None:
        self.hosts = hosts
        self.use: Use = use

    def run(self, image: str, *args: Any, **kw: Any) -> Any:
        p = self.hosts.place(self.use, image)
        return self.hosts.test_runner(p).run(image, *args, **kw)
