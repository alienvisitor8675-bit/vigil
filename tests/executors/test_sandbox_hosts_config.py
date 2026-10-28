"""ADR-0004 part 1: configs/sandbox_hosts.yaml and its model."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from vigild import config
from vigild.config import Config, ConfigError, SandboxHost, SandboxHostsFile


def _local() -> dict[str, Any]:
    return {
        "name": "local", "docker_context": "default", "arch": "arm64", "memory_gb": 10,
        "cpus": 4, "disk_budget_gb": 50, "uses": ["jobs", "bench"],
        "gateway_url": "http://host.docker.internal:8788",
    }  # fmt: skip


def _remote(**kw: Any) -> dict[str, Any]:
    base = {
        "name": "box-a", "docker_context": "sandbox-a", "arch": "amd64", "memory_gb": 16,
        "cpus": 8, "disk_budget_gb": 100, "uses": ["bench"], "engine_pin": "28.5.1",
        "gateway_url": "http://10.1.2.3:8788", "private_address": "10.1.2.4",
    }  # fmt: skip
    return {**base, **kw}


def _file(*hosts: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"bench": {"allow_emulation": True}, "hosts": list(hosts), **kw}


def test_default_config_has_only_local_and_allows_emulation(cfg: Config) -> None:
    sh = cfg.sandbox_hosts
    assert [h.name for h in sh.hosts] == ["local"]
    local = sh.hosts[0]
    assert local.docker_context == "default" and not local.remote
    assert local.arch == "arm64" and set(local.uses) == {"jobs", "bench"}
    assert local.gateway_url == "http://host.docker.internal:8788"
    assert sh.bench.allow_emulation is True
    assert not sh.remote_hosts()


def test_every_field_is_in_the_model() -> None:
    fields = set(SandboxHost.model_fields)
    assert {
        "name", "docker_context", "arch", "memory_gb", "cpus", "disk_budget_gb", "uses",
        "gateway_url", "engine_pin", "private_address",
    } <= fields  # fmt: skip


def test_a_remote_host_needs_a_pin_a_private_address_and_a_gateway_bind() -> None:
    SandboxHostsFile.model_validate(_file(_local(), _remote(), gateway_bind="10.1.2.3"))
    with pytest.raises(ValidationError, match="engine_pin"):
        SandboxHostsFile.model_validate(
            _file(_local(), _remote(engine_pin=None), gateway_bind="10.1.2.3")
        )
    with pytest.raises(ValidationError, match="private_address"):
        SandboxHostsFile.model_validate(
            _file(_local(), _remote(private_address=None), gateway_bind="10.1.2.3")
        )
    with pytest.raises(ValidationError, match="gateway_bind"):
        SandboxHostsFile.model_validate(_file(_local(), _remote()))


@pytest.mark.parametrize("addr", ["8.8.8.8", "127.0.0.1", "not-an-ip", "100.128.0.1", "172.32.0.1"])
def test_private_address_must_be_private(addr: str) -> None:
    with pytest.raises(ValidationError, match="private"):
        SandboxHostsFile.model_validate(
            _file(_local(), _remote(private_address=addr), gateway_bind="10.1.2.3")
        )


@pytest.mark.parametrize(
    "addr", ["10.0.0.1", "172.16.5.5", "192.168.1.9", "100.64.0.7", "100.127.1.1"]
)
def test_rfc1918_and_the_overlay_range_are_private(addr: str) -> None:
    SandboxHostsFile.model_validate(
        _file(_local(), _remote(private_address=addr), gateway_bind="10.1.2.3")
    )


def test_exactly_one_local_and_unique_names() -> None:
    with pytest.raises(ValidationError, match="local"):
        SandboxHostsFile.model_validate(_file(_remote()))
    with pytest.raises(ValidationError, match="unique"):
        SandboxHostsFile.model_validate(
            _file(_local(), _remote(), _remote(), gateway_bind="10.1.2.3")
        )


def test_the_local_entry_stays_on_the_default_context() -> None:
    with pytest.raises(ValidationError, match="local"):
        SandboxHostsFile.model_validate(_file({**_local(), "docker_context": "elsewhere"}))


def test_unknown_fields_and_uses_are_refused() -> None:
    with pytest.raises(ValidationError):
        SandboxHostsFile.model_validate(_file({**_local(), "uses": ["gpu"]}))
    with pytest.raises(ValidationError):
        SandboxHostsFile.model_validate(_file({**_local(), "tower": True}))


def test_load_reads_the_file(tmp_path: Any) -> None:
    import shutil

    shutil.copytree(config.CONFIG_DIR, tmp_path / "c")
    (tmp_path / "c" / "sandbox_hosts.yaml").unlink()
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / "c")
    assert isinstance(ConfigError("x"), ValueError)
