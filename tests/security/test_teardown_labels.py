"""The security suite's teardown touches only what its own run labelled (#26).
A fake Docker client stands in for the daemon: no docker package, no containers."""

from __future__ import annotations

from typing import Any

from tests.security.conftest import RUN_LABEL, SESSION_LABEL, _teardown_run


class NotFound(Exception):
    """Named like docker.errors.NotFound, which teardown recognises by name."""


class FakeContainer:
    def __init__(self, world: FakeWorld, cid: str, name: str, labels: dict[str, str]) -> None:
        self.world, self.id, self.name, self.labels = world, cid, name, labels

    def remove(self, force: bool = False) -> None:
        if self.id not in self.world.containers:
            raise NotFound(self.id)
        del self.world.containers[self.id]
        self.world.removed.append(self.id)


class FakeNetwork:
    def __init__(self, world: FakeWorld, nid: str, name: str, labels: dict[str, str]) -> None:
        self.world, self.id, self.name, self.labels = world, nid, name, labels
        self.members: list[str] = []

    @property
    def containers(self) -> list[FakeContainer]:
        return [self.world.containers[c] for c in self.members if c in self.world.containers]

    def reload(self) -> None:
        if self.id not in self.world.networks:
            raise NotFound(self.id)

    def disconnect(self, container: FakeContainer, force: bool = False) -> None:
        if container.id not in self.members:
            raise NotFound(container.id)
        self.members.remove(container.id)
        self.world.disconnected.append((self.id, container.id))

    def remove(self) -> None:
        if self.id not in self.world.networks:
            raise NotFound(self.id)
        del self.world.networks[self.id]
        self.world.removed.append(self.id)


class _Listing:
    def __init__(self, items: dict[str, Any]) -> None:
        self._items = items

    def list(self, filters: dict[str, str] | None = None, **kw: Any) -> list[Any]:
        # a name filter would be a bug in teardown: the fake supports labels only
        assert set(filters or {}) <= {"label"}, filters
        label = (filters or {}).get("label")
        out = list(self._items.values())
        if label is None:
            return out
        key, _, val = label.partition("=")
        return [o for o in out if o.labels.get(key) == val]


class FakeWorld:
    """A fake `docker.from_env()` client."""

    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}
        self.networks: dict[str, FakeNetwork] = {}
        self.removed: list[str] = []
        self.disconnected: list[tuple[str, str]] = []


class FakeClient:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    @property
    def containers(self) -> _Listing:
        return _Listing(self.world.containers)

    @property
    def networks(self) -> _Listing:
        return _Listing(self.world.networks)


def _add_container(w: FakeWorld, cid: str, name: str, **labels: str) -> FakeContainer:
    c = FakeContainer(w, cid, name, labels)
    w.containers[cid] = c
    return c


def _add_network(w: FakeWorld, nid: str, name: str, *members: str, **labels: str) -> FakeNetwork:
    n = FakeNetwork(w, nid, name, labels)
    n.members = list(members)
    w.networks[nid] = n
    return n


def _setup() -> tuple[FakeWorld, FakeClient]:
    w = FakeWorld()
    _add_container(w, "c-mine", "vigil-s-a-worker", **{SESSION_LABEL: "s-a"})
    _add_container(w, "c-run", "probe", **{RUN_LABEL: "run1"})
    _add_container(w, "c-other-run", "probe2", **{RUN_LABEL: "run2"})
    _add_container(w, "c-other-sid", "x", **{SESSION_LABEL: "s-z"})
    _add_network(w, "n-mine", "vigil-s-a", "c-mine", **{SESSION_LABEL: "s-a"})
    _add_network(w, "n-other", "vigil-s-z", "c-other-sid", **{SESSION_LABEL: "s-z"})
    return w, FakeClient(w)


def test_ac1_only_this_runs_labelled_resources_are_removed() -> None:
    w, client = _setup()
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    assert set(w.containers) == {"c-other-run", "c-other-sid"}
    assert set(w.networks) == {"n-other"}


def test_ac2_foreign_container_is_disconnected_not_removed() -> None:
    w, client = _setup()
    _add_container(w, "c-shared", "shared-service")  # no label at all
    w.networks["n-mine"].members.append("c-shared")
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    assert "c-shared" in w.containers  # still listed
    assert "c-shared" not in w.removed
    assert ("n-mine", "c-shared") in w.disconnected
    assert "n-mine" not in w.networks  # removed afterwards


def test_ac2_foreign_container_labelled_for_another_run_is_kept() -> None:
    w, client = _setup()
    w.networks["n-mine"].members.append("c-other-run")
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    assert "c-other-run" in w.containers
    assert ("n-mine", "c-other-run") in w.disconnected
    assert "n-mine" not in w.networks


def test_ac3_name_alone_does_not_select() -> None:
    w, client = _setup()
    _add_container(w, "c-named", "vigil-s-a-lookalike")  # right name, no label
    _add_network(w, "n-named", "vigil-s-a")  # right name, no label
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    assert "c-named" in w.containers
    assert "n-named" in w.networks


def test_ac4_second_run_removes_nothing_more() -> None:
    w, client = _setup()
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    removed, disconnected = list(w.removed), list(w.disconnected)
    _teardown_run(client, session_ids=["s-a"], run_id="run1")
    assert (w.removed, w.disconnected) == (removed, disconnected)


def test_ac4_resources_vanishing_mid_teardown_raise_nothing() -> None:
    w, _ = _setup()

    class Racy(FakeClient):
        @property
        def networks(self) -> _Listing:  # listed, then gone before reload/remove
            found = _Listing(self.world.networks).list(filters={"label": f"{SESSION_LABEL}=s-a"})
            self.world.networks.clear()
            return _Listing({n.id: n for n in found})

    _teardown_run(Racy(w), session_ids=["s-a"], run_id="run1")
    assert "c-mine" not in w.containers


def test_ac4_empty_world_is_a_no_op() -> None:
    w = FakeWorld()
    _teardown_run(FakeClient(w), session_ids=["s-a"], run_id="run1")
    assert w.removed == []
