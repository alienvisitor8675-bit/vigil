"""ADR-0004 rule 6: the disk budget, enforced by pruning only vigil-labelled
images and containers, least recently used first (#26)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.executors.hostfakes import GB, with_remote
from vigild.config import Config
from vigild.executors.disk import (
    CONTAINER_LABEL,
    IMAGE_LABEL,
    DiskBudgetExceeded,
    enforce_budget,
    usage_bytes,
)
from vigild.executors.hosts import HostRefused


def ANY(session: str) -> bool:
    return True


class _Item:
    def __init__(
        self, name: str, size: int, used: str, labels: dict[str, str], kind: str,
        status: str = "exited", image: str = "", role: str = "",
    ) -> None:  # fmt: skip
        self.name = name
        self.id = name
        self.labels = {**labels, **({"dev.vigil.role": role} if role else {})}
        labels = self.labels
        self.status = status
        started = "0001-01-01T00:00:00Z" if status == "created" else "2026-01-01T00:00:00Z"
        self.attrs = (
            {"Size": size, "Metadata": {"LastTagTime": used}, "Config": {"Labels": labels}}
            if kind == "image"
            else {
                "SizeRw": size,
                "Created": used,
                "Config": {"Labels": labels},
                "Image": image,
                "State": {"Status": status, "StartedAt": started},
            }
        )
        self.removed = False


class _Coll:
    def __init__(self, outer: Any, kind: str) -> None:
        self.outer, self.kind = outer, kind

    def _items(self) -> list[_Item]:
        return self.outer.items[self.kind]

    def list(self, **kw: Any) -> list[_Item]:
        label = kw.get("filters", {}).get("label")
        items = self.outer.every() if self.kind == "container" else self._items()
        return [i for i in items if not i.removed and (label is None or label in i.labels)]

    def remove(self, name: Any, **kw: Any) -> None:
        import docker.errors

        for i in self._items():
            if i.name == name:
                if self.kind == "container" and i.status in ("running", "paused", "restarting"):
                    self.outer.force_attempts.append(i.name) if kw.get("force") else None
                    if not kw.get("force"):
                        raise docker.errors.APIError("cannot remove a running container")
                if self.kind == "image" and any(
                    c.attrs["Image"] == i.id and not c.removed for c in self.outer.every()
                ):
                    raise docker.errors.APIError("image is being used by a container")
                i.removed = True
                self.outer.removed.append(i.name)


class FakeDisk:
    """A daemon holding labelled images, plus some that are not vigil's."""

    def __init__(
        self,
        images: list[tuple[str, int, str]] = (),  # type: ignore[assignment]
        containers: list[tuple[str, int, str]] = (),  # type: ignore[assignment]
        foreign: list[tuple[str, int, str]] = (),  # type: ignore[assignment]
        live: list[tuple[str, int, str, str]] = (),  # type: ignore[assignment]
        foreign_containers: list[tuple[str, str]] = (),  # type: ignore[assignment]
    ) -> None:
        from tests.executors.hostfakes import FakeHostClient

        self.base = FakeHostClient()
        self.removed: list[str] = []
        self.force_attempts: list[str] = []
        self.items = {
            "image": [_Item(n, s, u, {IMAGE_LABEL: n}, "image") for n, s, u in images]
            + [_Item(n, s, u, {}, "image") for n, s, u in foreign],
            "container": [
                _Item(n, s, u, {CONTAINER_LABEL: n}, "container") for n, s, u in containers
            ]
            # a running session container, and the image it was started from
            + [
                _Item(n, s, u, {CONTAINER_LABEL: n}, "container", "running", img)
                for n, s, u, img in live
            ]
            + [
                _Item(n, 0, "2026-01-01", {}, "container", "running", img)
                for n, img in foreign_containers
            ],
        }
        self.images = _Coll(self, "image")
        self.containers = _Coll(self, "container")

    def every(self) -> list[_Item]:
        return self.items["container"]

    def version(self) -> dict[str, Any]:
        return self.base.version()

    def info(self) -> dict[str, Any]:
        return self.base.info()

    def close(self) -> None:
        pass


def test_usage_counts_only_vigils_labelled_images_and_containers() -> None:
    d = FakeDisk(
        images=[("a", 3 * GB, "2026-01-01")],
        containers=[("c", GB, "2026-01-01")],
        foreign=[("other", 50 * GB, "2026-01-01")],
    )
    assert usage_bytes(d) == 4 * GB


def test_under_budget_prunes_nothing() -> None:
    d = FakeDisk(images=[("a", 3 * GB, "2026-01-01")])
    assert enforce_budget(d, 10 * GB, may_prune=ANY) == []


def test_over_budget_prunes_the_least_recently_used_first_until_under() -> None:
    d = FakeDisk(
        images=[("new", 4 * GB, "2026-03-01"), ("old", 4 * GB, "2026-01-01"),
                ("mid", 4 * GB, "2026-02-01")],
    )  # fmt: skip
    gone = enforce_budget(d, 9 * GB, may_prune=ANY)
    assert gone == ["old"] and d.removed == ["old"]


def test_it_never_touches_what_lacks_vigils_label() -> None:
    d = FakeDisk(
        images=[("mine", 6 * GB, "2026-01-01")], foreign=[("theirs", 90 * GB, "2025-01-01")]
    )
    enforce_budget(d, GB, may_prune=ANY)
    assert d.removed == ["mine"]  # the foreign image, though older and larger, stays


def test_stopped_containers_go_before_images_in_use_order() -> None:
    d = FakeDisk(images=[("img", 5 * GB, "2026-01-01")], containers=[("ctr", 5 * GB, "2025-12-01")])
    assert enforce_budget(d, 6 * GB, may_prune=ANY) == ["ctr"]


# --- round 2: the budget never touches live work -------------------------------------------


def test_a_running_labelled_session_container_survives_an_over_budget_prune() -> None:
    d = FakeDisk(
        images=[("img-live", 5 * GB, "2026-01-01"), ("img-old", 5 * GB, "2025-01-01")],
        containers=[("stopped", GB, "2026-02-01")],
        live=[("live", 5 * GB, "2025-01-01", "img-live")],
    )
    gone = enforce_budget(d, 10 * GB, may_prune=ANY)
    assert "live" not in d.removed and "img-live" not in d.removed
    assert d.force_attempts == []  # never force=True on a running container
    assert set(gone) == {"stopped", "img-old"}


def test_a_created_but_never_started_container_counts_as_stopped() -> None:
    d = FakeDisk(containers=[("c1", 3 * GB, "2026-01-01")])
    d.items["container"][0].status = "created"
    d.items["container"][0].attrs["State"] = {
        "Status": "created",
        "StartedAt": "0001-01-01T00:00:00Z",
    }
    assert enforce_budget(d, GB, may_prune=ANY) == ["c1"]


def test_an_image_a_container_uses_is_kept_even_when_that_container_is_not_vigils() -> None:
    d = FakeDisk(
        images=[("img-held", 8 * GB, "2025-01-01")], foreign_containers=[("theirs", "img-held")]
    )
    with pytest.raises(DiskBudgetExceeded):
        enforce_budget(d, GB, may_prune=ANY)
    assert d.removed == []


def test_an_image_is_freed_once_its_stopped_container_is_removed() -> None:
    d = FakeDisk(images=[("img", 8 * GB, "2025-01-01")], containers=[("c", GB, "2026-01-01")])
    d.items["container"][0].attrs["Image"] = "img"
    assert enforce_budget(d, GB, may_prune=ANY) == ["c", "img"]


def test_unmeetable_budget_refuses_with_a_reason_and_removes_no_live_work() -> None:
    d = FakeDisk(
        images=[("img-live", 9 * GB, "2026-01-01")], live=[("live", GB, "2026-01-01", "img-live")]
    )
    with pytest.raises(DiskBudgetExceeded, match="live work") as e:
        enforce_budget(d, 5 * GB, may_prune=ANY)
    assert isinstance(e.value, HostRefused)
    assert d.removed == [] and d.force_attempts == []


def test_a_session_on_an_over_budget_host_is_refused_before_anything_runs(rcfg: Config) -> None:
    from tests.executors.hostfakes import remote
    from vigild.executors.runtime import DockerRuntime

    d = FakeDisk(
        images=[("img-live", 900 * GB, "2026-01-01")],
        live=[("live", GB, "2026-01-01", "img-live")],
    )
    rt = DockerRuntime(rcfg, d, host=remote(rcfg))
    with pytest.raises(HostRefused, match="live work"):
        rt._prepare(None)  # type: ignore[arg-type]
    assert d.removed == []


def test_doctor_reports_live_work_that_blocks_the_budget(rcfg: Config) -> None:
    from tests.executors.hostfakes import remote
    from vigild.executors.hostcheck import _rule6

    d = FakeDisk(
        images=[("img-live", 900 * GB, "2026-01-01")],
        live=[("live", GB, "2026-01-01", "img-live")],
    )
    c = _rule6(remote(rcfg), d)
    assert c.ok is False and "live work" in c.note and "refused" in c.note


@pytest.fixture
def rcfg(cfg: Config) -> Config:
    return with_remote(cfg)


# --- round 2 additions: only work no live job needs is pruned ------------------------------


def _by_role(role: str) -> FakeDisk:
    d = FakeDisk(containers=[("c", 5 * GB, "2025-01-01")])
    item = d.items["container"][0]
    item.labels["dev.vigil.role"] = role
    item.attrs["Config"]["Labels"]["dev.vigil.role"] = role
    return d


@pytest.mark.parametrize("role", ["verifier", "deps"])
def test_a_verifier_or_deps_container_between_runs_is_never_pruned(role: str) -> None:
    d = _by_role(role)
    with pytest.raises(DiskBudgetExceeded):
        enforce_budget(d, GB, may_prune=ANY)  # even when the policy would allow it
    assert d.removed == []


def test_the_policy_decides_which_session_containers_may_go() -> None:
    d = FakeDisk(
        containers=[("live-job", 3 * GB, "2025-01-01"), ("final-job", 3 * GB, "2025-02-01")]
    )
    gone = enforce_budget(d, 3 * GB, may_prune=lambda sid: sid == "final-job")
    assert gone == ["final-job"] and d.removed == ["final-job"]


def _store_with(state: str, status: str = "done") -> tuple[Any, str]:
    from vigild.db import Store
    from vigild.jobs.model import JobState, Role

    store = Store.open(":memory:")
    job = store.create_job(repo="o/r", base="main", title="t", request="r", notes=[])
    store._force_state(job.id, JobState(state))
    s = store.create_session(job.id, Role.WORKER)
    store.update_session(s.id, status=status)
    return store, s.id


@pytest.mark.parametrize("state", ["merged", "done", "abandoned"])
def test_a_session_of_a_job_in_a_final_state_is_prunable(state: str) -> None:
    from vigild.executors.disk import prunable_sessions

    store, sid = _store_with(state)
    assert prunable_sessions(store)(sid) is True


@pytest.mark.parametrize("state", ["building", "in_review", "ready", "blocked", "escalated"])
def test_a_session_of_a_live_job_is_protected_so_its_result_can_still_be_imported(
    state: str,
) -> None:
    from vigild.executors.disk import prunable_sessions

    store, sid = _store_with(state)
    assert prunable_sessions(store)(sid) is False


def test_a_running_session_is_protected_even_in_a_final_job() -> None:
    from vigild.executors.disk import prunable_sessions

    store, sid = _store_with("merged", status="running")
    assert prunable_sessions(store)(sid) is False


def test_a_session_no_job_references_is_prunable() -> None:
    from vigild.db import Store
    from vigild.executors.disk import prunable_sessions

    assert prunable_sessions(Store.open(":memory:"))("s-unknown") is True


def test_the_server_wires_the_policy_into_the_hosts() -> None:
    from vigild.config import load
    from vigild.db import Store
    from vigild.server import build_executors

    cfg = load()
    _h, _r, _t = build_executors(cfg, object(), Store.open(":memory:"))
    assert _h.prune_ok is not None
