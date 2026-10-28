"""The gateway on a private interface (ADR-0004 §4).

When a remote sandbox host is configured, vigild also listens on its private
address (`gateway_bind`), and only then. That listener accepts a connection
only from a configured sandbox host's address; any other source is aborted in
`connection_made`, before a byte is parsed as HTTP. The private network's own
access rules are defence in depth, not the boundary. The per-session gateway
token is unchanged.
"""

from __future__ import annotations

from typing import Any

import uvicorn

from vigild.config import Config


def allowed_peers(cfg: Config) -> set[str]:
    return {h.private_address for h in cfg.sandbox_hosts.remote_hosts() if h.private_address}


def gated_protocol(allowed: set[str], base: type | None = None) -> type:
    """A uvicorn HTTP protocol class that drops every peer not in `allowed`."""
    if base is None:
        from uvicorn.protocols.http.h11_impl import H11Protocol

        base = H11Protocol
    permitted = frozenset(allowed)

    class Gated(base):  # type: ignore[valid-type, misc]
        def connection_made(self, transport: Any) -> None:
            peer = transport.get_extra_info("peername")
            ip = peer[0] if isinstance(peer, tuple) and peer else None
            if not isinstance(ip, str) or ip not in permitted:
                transport.abort()  # dropped: nothing is read, nothing is answered
                return
            super().connection_made(transport)

    return Gated


def private_config(cfg: Config, app: Any) -> uvicorn.Config | None:
    sh = cfg.sandbox_hosts
    if not sh.remote_hosts() or not sh.gateway_bind:
        return None
    return uvicorn.Config(
        app,
        host=sh.gateway_bind,
        port=cfg.vigild.gateway.port,
        log_level="info",
        http=gated_protocol(allowed_peers(cfg)),
    )
