"""vigild's own spans: one trace per session, the filter attributes on every span."""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx2
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vigild.telemetry.spans import Telemetry

ROOT = Path(__file__).resolve().parents[2]
ATTRS = {
    "vigil.task_id": "fx-0001-w1",
    "vigil.job_id": "fx-0001",
    "vigil.tier": "claude-sonnet",
    "vigil.model": "claude-sonnet-5-5",
    "vigil.repo": "o/fixture",
    "vigil.branch": "vigil/fx-0001-w1",
    "vigil.attempt": 1,
}


def test_session_trace_nests_spans_and_carries_attributes() -> None:
    exp = InMemorySpanExporter()
    tel = Telemetry(exporter=exp)
    with tel.session("s-1", ATTRS) as sess:
        assert sess.traceparent.startswith("00-") and len(sess.traceparent) == 55
        with tel.span(sess, "vigil.route", {"vigil.tier_reason": "rule9:default"}):
            pass
        with tel.span(sess, "vigil.spawn", {}):
            pass
        sess.set("vigil.pr_url", "https://github.com/o/r/pull/1")
    tel.flush()
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert set(spans) == {"vigil.session", "vigil.route", "vigil.spawn"}
    root = spans["vigil.session"]
    for name in ("vigil.route", "vigil.spawn"):
        assert spans[name].context.trace_id == root.context.trace_id
        assert spans[name].parent.span_id == root.context.span_id
    for s in spans.values():
        assert s.attributes["session.id"] == "s-1"
        for k, v in ATTRS.items():
            assert s.attributes[k] == v, (s.name, k)
    assert root.attributes["vigil.pr_url"] == "https://github.com/o/r/pull/1"
    assert f"{root.context.trace_id:032x}" in sess.traceparent


def test_without_an_endpoint_spans_are_dropped_quietly() -> None:
    tel = Telemetry(endpoint=None)
    with tel.session("s-2", {}) as sess, tel.span(sess, "vigil.route", {}):
        pass
    tel.flush()


@pytest.mark.docker
def test_spans_reach_phoenix_through_the_collector(tmp_path: Path) -> None:
    """Brings up compose.yaml under a throwaway project with generated secrets
    (not the real env file) and checks a vigild span lands in Phoenix."""
    env_file = tmp_path / "test.env"
    admin = "a1" + secrets.token_hex(20)
    env_file.write_text(
        f"PHOENIX_SECRET=s1{secrets.token_hex(20)}\nPHOENIX_ADMIN_SECRET={admin}\n"
        f"PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD=p1{secrets.token_hex(8)}\n"
        "VIGIL_COLLECTOR_NAME=vigiltest-collector\nVIGIL_OTLP_PORT=14318\nVIGIL_PHOENIX_PORT=16006\n"
    )
    env = {**os.environ, "PATH": f"{Path.home()}/.docker/bin:{os.environ['PATH']}"}
    up = [
        "docker",
        "compose",
        "-p",
        "vigiltest",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "compose.yaml"),
    ]
    subprocess.run(
        [*up, "up", "-d", "--build", "--wait"],
        check=True,
        env=env,
        capture_output=True,
        timeout=300,
    )
    try:
        for _ in range(60):  # wait for Phoenix to answer before sending
            try:
                if httpx2.get("http://127.0.0.1:16006/healthz", timeout=5).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(2)
        tel = Telemetry(endpoint="http://127.0.0.1:14318")
        with (
            tel.session("s-phoenix", {"vigil.task_id": "fx-9"}) as sess,
            tel.span(sess, "vigil.route", {}),
        ):
            pass
        tel.flush()
        names: set[str] = set()
        for _ in range(60):
            try:
                r = httpx2.get(
                    "http://127.0.0.1:16006/v1/projects/vigil/spans",
                    headers={"Authorization": f"Bearer {admin}"},
                    timeout=10,
                )
            except httpx2.HTTPError:
                time.sleep(2)
                continue
            if r.status_code == 200:
                names = {s["name"] for s in r.json().get("data", [])}
                if {"vigil.session", "vigil.route"} <= names:
                    break
            time.sleep(2)
        assert {"vigil.session", "vigil.route"} <= names
    finally:
        subprocess.run([*up, "down", "-v"], env=env, capture_output=True, timeout=120)
