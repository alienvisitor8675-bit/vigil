"""The dependency graph (plan 2a, item 2), without sessions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from vigild.scheduler.graph import Slots, run_graph


def _recorder(slow: dict[str, float] | None = None, fail: set[str] | None = None):  # type: ignore[no-untyped-def]
    log: list[tuple[str, str]] = []
    lock = threading.Lock()

    def make(n: str) -> Callable[[], bool]:
        def run() -> bool:
            with lock:
                log.append(("start", n))
            time.sleep((slow or {}).get(n, 0.01))
            with lock:
                log.append(("end", n))
            return n not in (fail or set())

        return run

    def launch(batch: list[str]) -> dict[str, Callable[[], bool]]:
        return {n: make(n) for n in batch}

    return log, launch


def _at(log: list[tuple[str, str]], ev: tuple[str, str]) -> int:
    return log.index(ev)


def test_a_diamond_runs_in_order() -> None:
    deps = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
    log, launch = _recorder()
    result = run_graph(deps, set(), launch)
    assert result.done == {"a", "b", "c", "d"} and not result.failed and not result.waiting
    assert _at(log, ("end", "a")) < _at(log, ("start", "b"))
    assert _at(log, ("end", "a")) < _at(log, ("start", "c"))
    assert _at(log, ("end", "b")) < _at(log, ("start", "d"))
    assert _at(log, ("end", "c")) < _at(log, ("start", "d"))
    # b and c ran at once
    assert _at(log, ("start", "c")) < _at(log, ("end", "b")) or _at(log, ("start", "b")) < _at(
        log, ("end", "c")
    )


def test_a_task_starts_when_its_own_dependencies_are_done() -> None:
    """Not waves: a slow, unrelated task holds up only its own dependents."""
    deps = {"a": [], "slow": [], "b": ["a"]}
    log, launch = _recorder(slow={"slow": 0.5})
    run_graph(deps, set(), launch)
    assert _at(log, ("start", "b")) < _at(log, ("end", "slow"))


def test_after_a_failure_nothing_new_starts() -> None:
    deps = {"a": [], "b": ["a"], "c": []}
    log, launch = _recorder(fail={"a"})
    result = run_graph(deps, set(), launch)
    assert result.failed == ["a"] and "b" in result.waiting
    assert ("start", "b") not in log


def test_a_refused_batch_halts_dispatch() -> None:
    deps = {"a": [], "b": ["a"]}
    _, launch = _recorder()
    calls: list[list[str]] = []

    def refuse_second(batch: list[str]) -> dict[str, Callable[[], bool]] | None:
        calls.append(batch)
        return launch(batch) if len(calls) == 1 else None

    result = run_graph(deps, set(), refuse_second)
    assert result.halted and result.done == {"a"} and result.waiting == ["b"]


def test_a_missing_dependency_leaves_the_task_waiting() -> None:
    log, launch = _recorder()
    result = run_graph({"a": ["gone"]}, set(), launch)
    assert result.waiting == ["a"] and not log


def test_slots_bound_sessions_across_callers() -> None:
    slots = Slots(3)

    def session() -> None:
        with slots.session():
            time.sleep(0.1)

    threads = [threading.Thread(target=session) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert slots.peak == 3
