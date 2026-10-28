"""A sandbox host's disk budget (ADR-0004 rule 6, #26).

vigild prunes only what carries its own label, least recently used first,
when usage passes `disk_budget_gb`. Images carry `dev.vigil.devimage` (every
dev and session image build sets it) and containers `dev.vigil.session`.
Nothing else on the host is listed, let alone removed. An image's last use is
its last tag time (its creation, when it has none): a container started from
it doesn't move it. Only stopped containers and unused images are ever
removed: live work is never touched, and a budget that can't be met without it
refuses the new session.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from vigild.executors.hosts import HostRefused

log = logging.getLogger("vigild")
IMAGE_LABEL = "dev.vigil.devimage"
CONTAINER_LABEL = "dev.vigil.session"
PROTECTED_ROLES = ("verifier", "deps")


def _labels(item: Any) -> dict[str, str]:
    return dict((item.attrs.get("Config") or {}).get("Labels") or {})


def _images(client: Any) -> list[Any]:
    return [
        i
        for i in client.images.list(filters={"label": IMAGE_LABEL})
        if IMAGE_LABEL in _labels(i) or IMAGE_LABEL in (getattr(i, "labels", None) or {})
    ]


def _containers(client: Any) -> list[Any]:
    found = [
        c
        for c in client.containers.list(all=True, filters={"label": CONTAINER_LABEL})
        if CONTAINER_LABEL in _labels(c) or CONTAINER_LABEL in (getattr(c, "labels", None) or {})
    ]
    # docker-py's `containers.list` takes no `size`; the low-level API does
    api = getattr(client, "api", None)
    if found and api is not None and hasattr(api, "containers"):
        sizes = {
            str(r.get("Id")): int(r.get("SizeRw") or 0)
            for r in api.containers(all=True, filters={"label": CONTAINER_LABEL}, size=True)
        }
        for c in found:
            if c.id in sizes:
                c.attrs["SizeRw"] = sizes[c.id]
    return found


def _size(item: Any) -> int:
    return int(item.attrs.get("Size") or item.attrs.get("SizeRw") or 0)


def _last_used(item: Any) -> str:
    meta = item.attrs.get("Metadata") or {}
    return str(meta.get("LastTagTime") or item.attrs.get("Created") or "")


def usage_bytes(client: Any) -> int:
    return sum(_size(i) for i in _images(client)) + sum(_size(c) for c in _containers(client))


def _stopped(c: Any) -> bool:
    """Exited, or created and never started. Running, paused and restarting
    containers are live work."""
    state = c.attrs.get("State") or {}
    status = str(state.get("Status") or getattr(c, "status", ""))
    if status == "exited":
        return True
    return status == "created" and str(state.get("StartedAt") or "").startswith("0001-01-01")


def _prunable(c: Any, may_prune: Callable[[str], bool]) -> bool:
    """A verifier or deps container is never pruned (it may sit between runs);
    a session's only when `may_prune` says its job is final or unknown."""
    labels = _labels(c) or dict(getattr(c, "labels", None) or {})
    if labels.get("dev.vigil.role") in PROTECTED_ROLES:
        return False
    return may_prune(str(labels.get(CONTAINER_LABEL, "")))


def prunable_sessions(store: Any) -> Callable[[str], bool]:
    """The pruning policy over vigild's own records: a session's container may
    go only when its job is merged, done or abandoned, or no job references the
    session. Anything else is live: its result may not be imported yet. A
    running session is protected whatever its job says."""
    from vigild.jobs.model import JobState

    final = (JobState.MERGED, JobState.DONE, JobState.ABANDONED)

    def ok(session_id: str) -> bool:
        try:
            s = store.get_session(session_id)
        except KeyError:
            return True
        if s.status in ("running", "pending", "reserved"):
            return False
        if s.job_id is None:
            return True
        try:
            return bool(store.get_job(s.job_id).state in final)
        except KeyError:
            return True

    return ok


def _in_use(client: Any) -> set[str]:
    """Image ids any container holds, whoever's it is and whatever its state."""
    return {str(c.attrs.get("Image")) for c in client.containers.list(all=True)}


def reclaimable_bytes(client: Any, may_prune: Callable[[str], bool] | None = None) -> int:
    """What a prune could free: vigil's stopped containers, and its images no
    container uses once those are gone."""
    stopped = [
        c
        for c in _containers(client)
        if _stopped(c) and _prunable(c, may_prune or (lambda sid: False))
    ]
    gone = {c.id for c in stopped}
    held = {str(c.attrs.get("Image")) for c in client.containers.list(all=True) if c.id not in gone}
    return sum(_size(c) for c in stopped) + sum(
        _size(i) for i in _images(client) if i.id not in held
    )


class DiskBudgetExceeded(HostRefused):
    """The budget can't be met without touching live work."""


def enforce_budget(
    client: Any, budget_bytes: int, *, may_prune: Callable[[str], bool]
) -> list[str]:
    """Remove vigil-labelled **stopped** containers, then vigil-labelled images
    no container uses, least recently used first, until usage is within the
    budget. Never a running container (no `force`), never an image in use.
    Raises `DiskBudgetExceeded` when what remains is live work over budget, so
    the new session is refused. Returns what was removed."""
    removed: list[str] = []
    used = usage_bytes(client)
    if used <= budget_bytes:
        return removed
    for c in sorted(
        (c for c in _containers(client) if _stopped(c) and _prunable(c, may_prune)),
        key=_last_used,
    ):
        if used <= budget_bytes:
            return removed
        if _remove(client.containers, c):
            used -= _size(c)
            removed.append(str(getattr(c, "name", c.id)))
    held = _in_use(client)  # after the containers went: an image may now be free
    for i in sorted((i for i in _images(client) if i.id not in held), key=_last_used):
        if used <= budget_bytes:
            return removed
        if _remove(client.images, i):
            used -= _size(i)
            removed.append(str(getattr(i, "name", i.id)))
    if used > budget_bytes:
        raise DiskBudgetExceeded(
            f"the disk budget is over by {(used - budget_bytes) / (1 << 30):.1f} GB and what "
            "is left is live work (running sessions, images they use); new session refused"
        )
    return removed


def _remove(coll: Any, item: Any) -> bool:
    try:
        coll.remove(item.id)
    except Exception as e:  # in use or already gone: leave it, try the next
        log.info("disk budget: %s not removed (%s)", item.id, type(e).__name__)
        return False
    return True
