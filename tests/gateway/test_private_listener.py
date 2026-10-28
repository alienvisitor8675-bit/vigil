"""ADR-0004 part 4: the gateway's private listener accepts only the configured
sandbox hosts' addresses and drops any other source before HTTP handling."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.executors.hostfakes import BIND, REMOTE_ADDR, with_remote
from vigild.config import Config
from vigild.gateway.app import create_gateway_app
from vigild.gateway.private import allowed_peers, gated_protocol, private_config


class FakeTransport:
    def __init__(self, peer: Any) -> None:
        self.peer = peer
        self.aborted = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self.peer if name == "peername" else default

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.aborted = True


class _Base:
    """Stands in for uvicorn's protocol: records that HTTP handling began."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.handled = False

    def connection_made(self, transport: Any) -> None:
        self.handled = True


def _proto(allowed: set[str]) -> Any:
    return gated_protocol(allowed, base=_Base)()


def test_a_listed_address_reaches_http_handling() -> None:
    p, t = _proto({REMOTE_ADDR}), FakeTransport((REMOTE_ADDR, 51000))
    p.connection_made(t)
    assert p.handled and not t.aborted


def test_an_unlisted_address_is_dropped_before_http_handling() -> None:
    p, t = _proto({REMOTE_ADDR}), FakeTransport(("10.9.9.9", 51000))
    p.connection_made(t)
    assert t.aborted and not p.handled


def test_no_peer_address_is_dropped() -> None:
    for peer in (None, ("", 0), "unix-socket"):
        p, t = _proto({REMOTE_ADDR}), FakeTransport(peer)
        p.connection_made(t)
        assert t.aborted and not p.handled


def test_an_empty_allowlist_drops_everyone() -> None:
    p, t = _proto(set()), FakeTransport((REMOTE_ADDR, 1))
    p.connection_made(t)
    assert t.aborted


def test_allowed_peers_are_the_remote_hosts_private_addresses(cfg: Config) -> None:
    assert allowed_peers(cfg) == set()
    with_remote(cfg)
    assert allowed_peers(cfg) == {REMOTE_ADDR}


def test_no_private_listener_without_a_remote_host(cfg: Config) -> None:
    assert private_config(cfg, FastAPI()) is None
    with_remote(cfg)
    conf = private_config(cfg, FastAPI())
    assert conf is not None and conf.host == BIND and conf.port == cfg.vigild.gateway.port


def test_the_gateway_has_a_health_route(cfg: Config) -> None:
    from vigild.gateway.service import Gateway

    gw: Any = Gateway.__new__(Gateway)
    r = TestClient(create_gateway_app(gw)).get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def _serve(allowed: set[str]) -> tuple[uvicorn.Server, int]:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", http=gated_protocol(allowed)
    )
    server = uvicorn.Server(conf)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, port


def _get(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
        c.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        c.settimeout(3)
        try:
            return c.recv(4096)
        except ConnectionResetError:
            return b""


def test_a_real_connection_from_an_unlisted_source_gets_no_http_answer() -> None:
    server, port = _serve({"10.9.9.9"})
    try:
        assert _get(port) == b""
    finally:
        server.should_exit = True


def test_a_real_connection_from_a_listed_source_is_served() -> None:
    server, port = _serve({"127.0.0.1"})
    try:
        assert b"200 OK" in _get(port)
    finally:
        server.should_exit = True
