"""ADR-0004: what a remote host changes in a container's spec, and what it
must not (the hardening is the same on every host)."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.executors.hostfakes import BIND, remote, with_remote
from vigild.config import Config
from vigild.executors.base import SessionSpec
from vigild.executors.egress import acl_yaml, allowlist, proxy_config_yaml
from vigild.executors.runtime import deps_kwargs, proxy_kwargs, verifier_kwargs, worker_kwargs
from vigild.jobs.model import Role


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        session_id="s-1", role=Role.WORKER, job_id="j", task_id="t", repo="o/r", preset="p",
        model="m", clone=tmp_path / "c", session_dir=tmp_path / "s", max_turns=1,
        max_minutes=1, max_budget_usd=1.0,
    )  # fmt: skip


def test_local_containers_set_no_cgroup_parent_and_keep_the_local_gateway(
    cfg: Config, tmp_path: Path
) -> None:
    host = cfg.sandbox_hosts.local
    for kw in (
        worker_kwargs(cfg, _spec(tmp_path), network="n", host=host),
        proxy_kwargs(cfg, _spec(tmp_path), host=host),
        verifier_kwargs(cfg, "img", tmp_path, "true", host=host),
        deps_kwargs(cfg, "img", tmp_path, network="n", host=host),
    ):
        assert "cgroup_parent" not in kw
    wk = worker_kwargs(cfg, _spec(tmp_path), network="n", host=host)
    assert wk["environment"]["VIGIL_GATEWAY_URL"] == "http://host.docker.internal:8788"


def test_every_container_on_a_remote_host_sets_the_hosts_slice(cfg: Config, tmp_path: Path) -> None:
    with_remote(cfg)
    host = remote(cfg)
    for kw in (
        worker_kwargs(cfg, _spec(tmp_path), network="n", host=host),
        proxy_kwargs(cfg, _spec(tmp_path), host=host),
        verifier_kwargs(cfg, "img", tmp_path, "true", host=host),
        verifier_kwargs(cfg, "img", tmp_path, "true", subject=tmp_path, kit=tmp_path, host=host),
        deps_kwargs(cfg, "img", tmp_path, network="n", host=host),
    ):
        assert kw["cgroup_parent"] == "vigil.slice"
        # the hardening carries over unchanged, and no device request, ever
        assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"]
        assert "no-new-privileges" in kw["security_opt"] and kw["privileged"] is False
        assert "device_requests" not in kw and "devices" not in kw and "runtime" not in kw


def test_a_remote_worker_reaches_the_hosts_gateway_url(cfg: Config, tmp_path: Path) -> None:
    with_remote(cfg)
    wk = worker_kwargs(cfg, _spec(tmp_path), network="n", host=remote(cfg))
    assert wk["environment"]["VIGIL_GATEWAY_URL"] == f"http://{BIND}:8788"


def test_the_remote_sidecars_allowlist_is_exactly_the_hosts_gateway(cfg: Config) -> None:
    with_remote(cfg)
    host = remote(cfg)
    assert allowlist(cfg, Role.WORKER, host)[-1] == BIND
    assert BIND not in allowlist(cfg, Role.WORKER)
    conf = yaml.safe_load(proxy_config_yaml(cfg, host))
    assert conf["allow_addresses"] == [f"{BIND}:8788"]
    acl = yaml.safe_load(acl_yaml(allowlist(cfg, Role.COORD, host)))
    assert acl["default"]["allowed_domains"] == [BIND]
    # the local default is untouched
    assert yaml.safe_load(proxy_config_yaml(cfg))["allow_addresses"] == ["192.168.65.254:8788"]
