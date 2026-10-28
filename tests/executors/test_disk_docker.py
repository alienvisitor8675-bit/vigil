"""ADR-0004 rule 6 against real Docker (Opus review, round 2): the budget code's
calls must exist in docker-py, which the fakes can't show."""

from __future__ import annotations

import uuid

import pytest

from vigild.executors.disk import CONTAINER_LABEL, reclaimable_bytes, usage_bytes

pytestmark = pytest.mark.docker

IMAGE = "vigil/egress-proxy:0.1.0"  # built by scripts/build-images.py; nothing is pulled


def test_usage_and_reclaimable_run_on_real_docker() -> None:
    import docker

    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{IMAGE} is not built here")
    run = f"disk-{uuid.uuid4().hex[:8]}"
    c = client.containers.create(
        IMAGE, command="true", labels={CONTAINER_LABEL: run, "vigil.test-run": run}
    )
    try:
        used = usage_bytes(client)
        free = reclaimable_bytes(client, lambda session: session == run)
        assert isinstance(used, int) and used >= 0
        assert isinstance(free, int) and free >= 0
    finally:
        c.remove(force=True)  # this test's own container, by id
