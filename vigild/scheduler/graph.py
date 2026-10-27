"""Run a job's tasks as a dependency graph (plan 2a, item 2).

A task starts as soon as every task it depends on is done, not when a whole
wave is: a slow task holds up only its own dependents. Tasks that become
ready together are admitted together (`launch` reserves their budgets as one
batch, all or none, B14). After a failure, or when `launch` halts (a budget
refusal, a tripped breaker, the WIP limit), nothing new starts; what's
running finishes, and the caller decides what the job does next.

The graph never runs sessions itself: each task's runner takes one of the
scheduler's session slots (`Slots`) for its session, so the number of
sessions at once is bounded across every job, not per job.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field

Launch = Callable[[list[str]], dict[str, Callable[[], bool]] | None]


@dataclass
class GraphResult:
    done: set[str]
    failed: list[str] = field(default_factory=list)
    halted: bool = False  # `launch` refused a batch
    waiting: list[str] = field(default_factory=list)  # never started


def run_graph(deps: Mapping[str, Collection[str]], done: set[str], launch: Launch) -> GraphResult:
    """Run every node not in `done`, each once its dependencies are done.
    `launch(batch)` returns a callable (True on success) for each node of the
    batch it admits now; a node it leaves out is offered again after the next
    one finishes. None halts dispatch."""
    done = set(done)
    order = [n for n in deps if n not in done]
    started: set[str] = set()
    failed: list[str] = []
    halted = False
    running: dict[Future[bool], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(order))) as pool:
        while True:
            if not failed and not halted:
                batch = [n for n in order if n not in started and set(deps[n]) <= done]
                if batch:
                    fns = launch(batch)
                    if fns is None:
                        halted = True
                    else:
                        for n, fn in fns.items():
                            started.add(n)
                            running[pool.submit(fn)] = n
            if not running:
                break
            finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for f in finished:
                n = running.pop(f)
                if f.result():
                    done.add(n)
                else:
                    failed.append(n)
    waiting = [n for n in order if n not in started]
    return GraphResult(done=done, failed=failed, halted=halted, waiting=waiting)


class Slots:
    """At most `n` sessions at once, across every job (coord, worker,
    reviewer, auditor). Verification has its own one-at-a-time slot (B33)."""

    def __init__(self, n: int) -> None:
        self.n = n
        self._sem = threading.BoundedSemaphore(n)
        self._lock = threading.Lock()
        self.in_use = 0
        self.peak = 0

    @contextmanager
    def session(self) -> Iterator[None]:
        with self._sem:
            with self._lock:
                self.in_use += 1
                self.peak = max(self.peak, self.in_use)
            try:
                yield
            finally:
                with self._lock:
                    self.in_use -= 1
