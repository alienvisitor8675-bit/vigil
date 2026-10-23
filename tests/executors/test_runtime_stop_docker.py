"""DockerRuntime.stop kills a live session's container (docker; run by the owner)."""

from __future__ import annotations

import time

import pytest

from vigild.config import Config
from vigild.executors.runtime import LABEL, DockerRuntime

pytestmark = pytest.mark.docker


def test_stop_kills_the_sessions_container(cfg: Config) -> None:
    rt = DockerRuntime(cfg)
    sid = "s-stop-test"
    c = rt.client.containers.run(
        cfg.vigild.images.claude_executor,
        entrypoint=["sleep", "600"],
        detach=True,
        labels={LABEL: sid},
    )
    try:
        assert rt.client.containers.list(filters={"label": f"{LABEL}={sid}"})
        rt.stop(sid)
        for _ in range(50):
            if not rt.client.containers.list(filters={"label": f"{LABEL}={sid}"}):
                break
            time.sleep(0.1)
        assert not rt.client.containers.list(filters={"label": f"{LABEL}={sid}"})
    finally:
        c.remove(force=True)
