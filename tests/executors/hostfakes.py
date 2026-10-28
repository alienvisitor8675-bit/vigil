"""Fakes for sandbox-host tests: a config with a remote host, and Docker
clients that record what is asked of them (ADR-0004). Nothing here touches a
network or a machine."""

from __future__ import annotations

from typing import Any, ClassVar

from vigild.config import Config, SandboxHost, SandboxHostsFile

REMOTE_ADDR = "10.1.2.4"
BIND = "10.1.2.3"
PIN = "28.5.1"


def remote_entry(**kw: Any) -> dict[str, Any]:
    base = {
        "name": "box-a", "docker_context": "sandbox-a", "arch": "amd64", "memory_gb": 16,
        "cpus": 8, "disk_budget_gb": 100, "uses": ["bench"], "engine_pin": PIN,
        "gateway_url": f"http://{BIND}:8788", "private_address": REMOTE_ADDR,
        "cgroup_slice": "vigil.slice",
    }  # fmt: skip
    return {**base, **kw}


def with_remote(cfg: Config, *, allow_emulation: bool = True, **kw: Any) -> Config:
    local = cfg.sandbox_hosts.local.model_dump()
    cfg.sandbox_hosts = SandboxHostsFile.model_validate(
        {
            "bench": {"allow_emulation": allow_emulation},
            "gateway_bind": BIND,
            "hosts": [local, remote_entry(**kw)],
        }
    )
    return cfg


def remote(cfg: Config) -> SandboxHost:
    return cfg.sandbox_hosts.remote_hosts()[0]


class FakeClient:
    """A Docker client for one host. `calls` records every build or pull."""

    def __init__(self, name: str = "fake", images: dict[str, str] | None = None) -> None:
        self.name = name
        self.arch_of = images or {}
        self.calls: list[tuple[str, str]] = []
        self.images = self._Images(self)
        self.containers = self._Containers()

    class _Containers:
        def list(self, **kw: Any) -> list[Any]:
            return []

    class _Images:
        def __init__(self, outer: FakeClient) -> None:
            self.outer = outer

        def get(self, image: str) -> Any:
            import docker.errors

            if image not in self.outer.arch_of:
                raise docker.errors.ImageNotFound(image)
            arch = self.outer.arch_of[image]

            class Img:
                id = "sha256:" + "a" * 64
                attrs: ClassVar[dict[str, Any]] = {"Architecture": arch}

            return Img()

        def list(self, **kw: Any) -> list[Any]:
            return []

        def pull(self, image: str, **kw: Any) -> None:
            self.outer.calls.append(("pull", image))

        def build(self, **kw: Any) -> None:
            self.outer.calls.append(("build", str(kw)))

    def close(self) -> None:
        pass


GB = 1 << 30


class FakeHostClient(FakeClient):
    """A Docker daemon as `docker info` and `docker version` describe it."""

    def __init__(
        self, *, version: str = PIN, security: list[str] | None = None, arch: str = "x86_64",
        default_runtime: str = "runc",
    ) -> None:  # fmt: skip
        super().__init__("host")
        self._version = version
        self._info = {
            "SecurityOptions": security
            if security is not None
            else ["name=seccomp,profile=builtin", "name=userns"],
            "Architecture": arch,
            "DefaultRuntime": default_runtime,
            "Runtimes": {"runc": {}},
        }

    def version(self) -> dict[str, Any]:
        return {"Version": self._version}

    def info(self) -> dict[str, Any]:
        return self._info


class FakeSsh:
    """A fake `ssh <key> <plain shell>`: refused (as a restricted key is) or not."""

    def __init__(self, refused: bool = True) -> None:
        self.refused = refused
        self.asked: list[str] = []

    def shell_refused(self, endpoint: str) -> tuple[bool, str]:
        self.asked.append(endpoint)
        return self.refused, "plain shell refused" if self.refused else "a plain shell ran"
