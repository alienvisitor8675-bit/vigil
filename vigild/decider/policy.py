"""Decider policy: act at >= 0.85, escalate to Haiku at >= 0.6, else wait for
the owner. Only routes the repo lists in configs/repos.yaml are asked.
Every answer is written to `decisions` with the action taken."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from vigild.config import Config
from vigild.db import Store, iso
from vigild.decider.questions import Answer, DeciderError, Question
from vigild.gateway.service import GatewayError
from vigild.provenance import Provenance
from vigild.repos import RepoPolicyError, check_decider_route

ACT = 0.85
ESCALATE = 0.6
HAIKU = "haiku"


class Action(StrEnum):
    ACT = "act"
    ESCALATED = "escalated"
    WAIT = "wait"


class Backend(Protocol):
    name: str

    def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer: ...


@dataclass(frozen=True)
class Decision:
    answer: Answer
    action: Action


NO_ANSWER = "no-answer"


class Decider:
    def __init__(self, cfg: Config, store: Store, backends: Mapping[str, Backend]) -> None:
        self.cfg = cfg
        self.store = store
        self.backends = dict(backends)

    def _record(self, a: Answer, action: Action, job_id: str | None, task_id: str | None) -> None:
        # Hosted non-Claude output (Jev) has no provenance value of its own; it
        # is tagged `claude`, the conservative tag the training exporter refuses.
        route = self.cfg.backends.decider_routes.get(a.backend)
        local = route is not None and route.locality == "local"
        prov = Provenance.LOCAL if local else Provenance.CLAUDE
        self.store.x(
            "INSERT INTO decisions (job_id, task_id, question, answer, probability, backend,"
            " model_version, action, ts, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task_id,
                a.question,
                str(a.value),
                a.probability,
                a.backend,
                a.model_version,
                action.value,
                iso(),
                prov.value,
            ),
        )

    def _ask(
        self, route: str, q: Question, state: str, repo: str, job_id: str | None
    ) -> Answer | None:
        try:
            check_decider_route(self.cfg, repo, route)  # item 11: never a route the repo omits
            return self.backends[route].ask(q, state, repo=repo, job_id=job_id)
        except (DeciderError, RepoPolicyError, GatewayError) as e:  # a refusal is a failure
            self.store.add_event(
                "decision",
                {"route": route, "question": q.name, "error": str(e)},
                Provenance.HUMAN,
                job_id=job_id,
            )
            return None

    def decide(
        self,
        repo: str,
        q: Question,
        state: str,
        *,
        job_id: str | None = None,
        task_id: str | None = None,
    ) -> Decision:
        listed = [r for r in self.cfg.repo(repo).decider if r in self.backends]
        primary = next((r for r in listed if r != HAIKU), None) or (
            HAIKU if HAIKU in listed else None
        )
        if primary is None:
            return self._wait(q, job_id, task_id)
        a = self._ask(primary, q, state, repo, job_id)
        if a is None:
            if primary != HAIKU and HAIKU in listed:
                return self._via_haiku(q, state, repo, job_id, task_id)
            return self._wait(q, job_id, task_id)
        if a.probability >= ACT:
            self._record(a, Action.ACT, job_id, task_id)
            return Decision(a, Action.ACT)
        if primary != HAIKU and a.probability >= ESCALATE and HAIKU in listed:
            self._record(a, Action.ESCALATED, job_id, task_id)
            return self._via_haiku(q, state, repo, job_id, task_id)
        self._record(a, Action.WAIT, job_id, task_id)
        return Decision(a, Action.WAIT)

    def _via_haiku(
        self, q: Question, state: str, repo: str, job_id: str | None, task_id: str | None
    ) -> Decision:
        h = self._ask(HAIKU, q, state, repo, job_id)
        if h is None:
            return self._wait(q, job_id, task_id)
        action = Action.ACT if h.probability >= ACT else Action.WAIT
        self._record(h, action, job_id, task_id)
        return Decision(h, action)

    def _wait(self, q: Question, job_id: str | None, task_id: str | None) -> Decision:
        a = Answer(q.name, None, 0.0, NO_ANSWER)
        self.store.x(
            "INSERT INTO decisions (job_id, task_id, question, answer, probability, backend,"
            " model_version, action, ts, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task_id,
                q.name,
                "",
                0.0,
                NO_ANSWER,
                None,
                Action.WAIT.value,
                iso(),
                Provenance.HUMAN.value,
            ),
        )
        return Decision(a, Action.WAIT)
