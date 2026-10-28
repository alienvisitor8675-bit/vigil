"""ADR-0004 part 3: the six rules a remote sandbox host must meet, one doctor
line per host per rule, and vigild refusing to start when one fails. All
offline: a fake Docker daemon, a fake SSH probe and a fake probe container."""

from __future__ import annotations

from typing import Any

import pytest

from tests.executors.hostfakes import (
    BIND,
    GB,
    REMOTE_ADDR,
    FakeHostClient,
    FakeSsh,
    with_remote,
)
from vigild.config import Config
from vigild.executors.hostcheck import (
    HostRuleError,
    ProbeReport,
    check_hosts,
    egress_targets,
    require_hosts,
)
from vigild.executors.hosts import Hosts

ENDPOINT = "ssh://sandbox@box-a.example"


def good_report(cfg: Config, **kw: Any) -> ProbeReport:
    host = cfg.sandbox_hosts.remote_hosts()[0]
    base: dict[str, Any] = {
        "gateway_health": True,
        "reached": {name: False for name in egress_targets(cfg, host)},
        "gpu_nodes": [],
        "memory_max": str(int(host.memory_gb * GB)),
        "cpu_max": f"{int(host.cpus * 100000)} 100000",
    }
    return ProbeReport(**{**base, **kw})


class FakeProber:
    def __init__(self, report: ProbeReport | Exception) -> None:
        self.report = report
        self.hosts: list[str] = []

    def probe(self, host: Any) -> ProbeReport:
        self.hosts.append(host.name)
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def run(
    cfg: Config, *, client: FakeHostClient | None = None, ssh: FakeSsh | None = None,
    report: ProbeReport | None = None, endpoint: str = ENDPOINT, address: str = REMOTE_ADDR,
    prober: FakeProber | None = None,
) -> dict[str, Any]:  # fmt: skip
    cl = client or FakeHostClient()
    hosts = Hosts(cfg, make_client=lambda h: cl)
    checks = check_hosts(
        cfg,
        hosts,
        ssh=ssh or FakeSsh(),
        resolve=lambda name: [address],
        prober_for=lambda host, client: prober or FakeProber(report or good_report(cfg)),
        lookup=lambda ctx: endpoint,
    )
    return {c.name: c for c in checks}


@pytest.fixture
def rcfg(cfg: Config) -> Config:
    return with_remote(cfg)


def rule(checks: dict[str, Any], n: int) -> Any:
    (hit,) = [c for name, c in checks.items() if f"rule {n}" in name and "box-a" in name]
    return hit


def test_a_good_host_has_one_passing_line_per_rule(rcfg: Config) -> None:
    checks = run(rcfg)
    box = [c for n, c in checks.items() if "box-a" in n]
    assert len(box) == 6 and all(c.ok for c in box), box
    assert all(n.startswith("sandbox host box-a: rule ") for n in checks if "box-a" in n)
    assert not [n for n in checks if "box-a" not in n and "rule" in n]  # local has no rules here


def test_local_alone_has_no_remote_rules(cfg: Config) -> None:
    checks = run(cfg)
    assert not [n for n in checks if "rule" in n]


# --- rule 1 ----------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["tcp://10.1.2.4:2375", "unix:///x.sock", ""])
def test_rule1_a_tcp_endpoint_is_refused(rcfg: Config, endpoint: str) -> None:
    c = rule(run(rcfg, endpoint=endpoint), 1)
    assert c.ok is False and "ssh://" in c.note


def test_rule1_the_target_must_resolve_to_a_private_address(rcfg: Config) -> None:
    c = rule(run(rcfg, address="8.8.8.8"), 1)
    assert c.ok is False and "private" in c.note
    assert rule(run(rcfg, address="100.100.1.1"), 1).ok is True  # the overlay range


def test_rule1_a_plain_shell_must_be_refused(rcfg: Config) -> None:
    ssh = FakeSsh(refused=False)
    c = rule(run(rcfg, ssh=ssh), 1)
    assert c.ok is False and "shell" in c.note
    assert ssh.asked == [ENDPOINT]
    assert rule(run(rcfg, ssh=FakeSsh(refused=True)), 1).ok is True


# --- rule 2 ----------------------------------------------------------------------


def test_rule2_the_engine_must_equal_the_hosts_pin(rcfg: Config) -> None:
    c = rule(run(rcfg, client=FakeHostClient(version="29.0.0")), 2)
    assert c.ok is False and "pin" in c.note


def test_rule2_userns_must_be_in_the_security_options(rcfg: Config) -> None:
    c = rule(run(rcfg, client=FakeHostClient(security=["name=seccomp,profile=builtin"])), 2)
    assert c.ok is False and "userns" in c.note


def test_rule2_rootless_is_not_accepted(rcfg: Config) -> None:
    sec = ["name=seccomp,profile=builtin", "name=rootless", "name=userns"]
    c = rule(run(rcfg, client=FakeHostClient(security=sec)), 2)
    assert c.ok is False and "rootless" in c.note


def test_rule2_the_arch_must_match_the_config(rcfg: Config) -> None:
    c = rule(run(rcfg, client=FakeHostClient(arch="aarch64")), 2)
    assert c.ok is False and "arch" in c.note


# --- rule 3 ----------------------------------------------------------------------


def test_the_egress_targets_cover_the_internet_the_host_the_private_net_and_every_model(
    rcfg: Config,
) -> None:
    t = egress_targets(rcfg, rcfg.sandbox_hosts.remote_hosts()[0])
    names = set(t)
    assert any(n.startswith("internet") for n in names)
    assert any(n.startswith("host-port") for n in names)
    assert any(n.startswith("private") for n in names)
    for preset in rcfg.backends.presets.values():
        from urllib.parse import urlsplit

        assert any(urlsplit(preset.base_url).hostname == h for h, _ in t.values())
    assert (REMOTE_ADDR, 22) in t.values()  # the host's own other ports
    assert (BIND, 8787) in t.values()  # vigild's API port is not the gateway's


def test_rule3_the_probe_must_reach_the_gateway_health_route(rcfg: Config) -> None:
    c = rule(run(rcfg, report=good_report(rcfg, gateway_health=False)), 3)
    assert c.ok is False and "gateway" in c.note


def test_rule3_any_reachable_target_fails(rcfg: Config) -> None:
    host = rcfg.sandbox_hosts.remote_hosts()[0]
    for name in egress_targets(rcfg, host):
        reached = {n: False for n in egress_targets(rcfg, host)} | {name: True}
        c = rule(run(rcfg, report=good_report(rcfg, reached=reached)), 3)
        assert c.ok is False and name in c.note


def test_rule3_a_target_the_probe_did_not_try_fails(rcfg: Config) -> None:
    host = rcfg.sandbox_hosts.remote_hosts()[0]
    reached = {n: False for n in list(egress_targets(rcfg, host))[1:]}
    assert rule(run(rcfg, report=good_report(rcfg, reached=reached)), 3).ok is False


def test_a_probe_that_cannot_run_fails_rules_3_to_5(rcfg: Config) -> None:
    checks = run(rcfg, prober=FakeProber(RuntimeError("no image")))
    for n in (3, 4, 5):
        c = rule(checks, n)
        assert c.ok is False and "RuntimeError" in c.note


# --- rule 4 ----------------------------------------------------------------------


def test_rule4_a_gpu_default_runtime_fails(rcfg: Config) -> None:
    c = rule(run(rcfg, client=FakeHostClient(default_runtime="nvidia")), 4)
    assert c.ok is False and "runtime" in c.note


def test_rule4_a_gpu_device_node_in_the_probe_fails(rcfg: Config) -> None:
    c = rule(run(rcfg, report=good_report(rcfg, gpu_nodes=["/dev/nvidia0"])), 4)
    assert c.ok is False and "nvidia" in c.note


# --- rule 5 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"memory_max": None},
        {"memory_max": "max"},
        {"memory_max": str(17 * GB)},
        {"cpu_max": None},
        {"cpu_max": "max 100000"},
        {"cpu_max": "900000 100000"},
    ],
)
def test_rule5_missing_or_larger_slice_limits_fail(rcfg: Config, kw: dict[str, Any]) -> None:
    c = rule(run(rcfg, report=good_report(rcfg, **kw)), 5)
    assert c.ok is False


def test_rule5_smaller_limits_pass(rcfg: Config) -> None:
    assert rule(run(rcfg, report=good_report(rcfg, memory_max=str(8 * GB))), 5).ok is True


# --- rule 6 ----------------------------------------------------------------------


def test_rule6_reports_usage_against_the_budget(rcfg: Config) -> None:
    c = rule(run(rcfg), 6)
    assert c.ok is True and "GB of 100 GB" in c.note


def test_rule6_usage_over_the_budget_fails(rcfg: Config) -> None:
    from tests.executors.test_disk import FakeDisk

    cl = FakeDisk(images=[("a", 80 * GB, "2026-01-01"), ("b", 40 * GB, "2026-02-01")])
    c = rule(run(rcfg, client=cl), 6)  # type: ignore[arg-type]
    assert c.ok is False and "over" in c.note


# --- start-up --------------------------------------------------------------------


def _hosts(cfg: Config, cl: FakeHostClient) -> Hosts:
    return Hosts(cfg, make_client=lambda h: cl)


def test_vigild_refuses_to_start_when_a_remote_host_fails_a_rule(rcfg: Config) -> None:
    with pytest.raises(HostRuleError, match="rule 2") as e:
        require_hosts(
            rcfg,
            _hosts(rcfg, FakeHostClient(version="0.0.1")),
            ssh=FakeSsh(),
            resolve=lambda n: [REMOTE_ADDR],
            prober_for=lambda h, c: FakeProber(good_report(rcfg)),
            lookup=lambda c: ENDPOINT,
        )
    assert "box-a" in str(e.value)


def test_vigild_starts_when_every_rule_passes_and_with_no_remote_host(rcfg: Config) -> None:
    kw: dict[str, Any] = dict(
        ssh=FakeSsh(), resolve=lambda n: [REMOTE_ADDR],
        prober_for=lambda h, c: FakeProber(good_report(rcfg)), lookup=lambda c: ENDPOINT,
    )  # fmt: skip
    require_hosts(rcfg, _hosts(rcfg, FakeHostClient()), **kw)

    def boom(h: Any) -> Any:
        raise AssertionError("no remote host: nothing to reach")

    from vigild import config

    local_only = config.load()
    require_hosts(local_only, Hosts(local_only, make_client=boom), **kw)
