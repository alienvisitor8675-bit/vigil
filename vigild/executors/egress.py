"""Per-session egress (B4, B5): an allowlist from the session's role, written
as a smokescreen ACL. The gateway is the only model endpoint, and the only
private address the proxy will connect to."""

from __future__ import annotations

from urllib.parse import urlsplit

import yaml

from vigild.config import Config, SandboxHost
from vigild.jobs.model import Role


def _gateway(host: SandboxHost) -> tuple[str, int]:
    u = urlsplit(host.gateway_url)
    if not u.hostname or not u.port:
        raise ValueError(f"{host.name}: gateway_url needs a host and a port")
    return u.hostname, u.port


def allowlist(cfg: Config, role: Role, host: SandboxHost | None = None) -> list[str]:
    hosts = list(cfg.egress.roles.get(role.value, []))
    if role is Role.DEPS:
        return hosts  # registries only: no gateway, no model (B39)
    if host is not None and host.remote:
        return [*hosts, _gateway(host)[0]]  # exactly the host's gateway_url (ADR-0004)
    return [*hosts, cfg.egress.gateway.host]


def acl_yaml(domains: list[str]) -> str:
    acl = {
        "version": "v1",
        "services": [],
        "default": {
            "name": "default",
            "project": "vigil",
            "action": "enforce",
            "allowed_domains": list(domains),
        },
    }
    return "---\n" + str(yaml.safe_dump(acl, sort_keys=False))


def proxy_config_yaml(cfg: Config, host: SandboxHost | None = None) -> str:
    allow = cfg.egress.gateway.allow_address
    if host is not None and host.remote:
        name, port = _gateway(host)
        allow = f"{name}:{port}"
    conf = {
        # Sessions are told apart by network, not by TLS client certificates,
        # so every request falls to the ACL's default policy.
        "allow_missing_role": True,
        "allow_addresses": [allow],
    }
    return "---\n" + str(yaml.safe_dump(conf, sort_keys=False))
