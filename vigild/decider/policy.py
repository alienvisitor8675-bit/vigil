"""Decider policy: act at >= 0.85, escalate to Haiku at >= 0.6, else wait for
the owner. Only routes the repo's chain resolves to are asked, by its live
visibility (`jev`: OpenRouter for a public repo, Cloudflare for a private
one, `vigild/repos.py`). Every answer is written to `decisions` with the
action taken and the model version it reported ("unknown" when none), and
goes on a `vigil.decider` span with the route and the visibility.

Every decision reads the repo's visibility once and uses that answer for
every call it makes, the Jev call and any Haiku call after it (review A10). A
lookup that fails counts as private: the Cloudflare route, never OpenRouter.

When Jev fails, the chain falls to Haiku (if listed), booked as
`decider_fallback`, apart from a normal 0.6-0.85 escalation (`decider`), so a
failing Jev can't quietly eat the day; the gateway caps fallback spend per
repo per day (`decider_fallback_per_repo_usd`, owner ruling 4). Two Jev
failures page the owner, once each:
- **version drift** (a version other than the route's pin, or none): one
  inbox item per new (route, version), deduplicated in `jev_drift` and
  written in the same transaction as its page (review A9);
- **Cloudflare 2021** (the AI Gateway credit is empty): the credit breaker
  trips and pages. Like drift, the page says where decisions went ("on Haiku
  fallback" or "waiting for the owner"), and every Haiku decision for a repo
  whose Jev is that route, while the breaker is open, is a fallback (review
  A2), until the owner resets it.

Each row in `decisions` and each span carries the question's type and the
answer's confidence (owner ruling 6; the report is `vigild/kpis.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from vigild.config import Config
from vigild.db import Store, iso, utcnow
from vigild.decider.questions import QUESTIONS, Answer, DeciderError, Question
from vigild.gateway.service import FALLBACK_PURPOSE, GatewayError
from vigild.provenance import Provenance
from vigild.repos import RepoPolicyError, check_decider_route, live_private, resolve_decider

ACT = 0.85
ESCALATE = 0.6
HAIKU = "haiku"
DECIDER, FALLBACK = "decider", FALLBACK_PURPOSE  # the two purposes a decider call books
CREDIT_ROUTE = "jev-cloudflare"
QUESTIONS_KIND = {name: q.kind for name, q in QUESTIONS.items()}


def decider_routes(
    cfg: Config, repo: str, available: Collection[str], *, private: bool
) -> tuple[str | None, str | None]:
    """(primary, escalation) for a repo at this visibility: its first route
    other than Haiku that vigild has built (`jev` resolved by visibility), with
    Haiku as the escalation when listed; Haiku alone when no Jev route is
    available. `decide` and `vigil doctor` both read this (owner, 2026-10-23)."""
    listed = []
    for r in resolve_decider(cfg, repo, private):
        if r not in available:
            continue
        try:
            check_decider_route(
                cfg, repo, r, private=private
            )  # e.g. no Haiku for most private repos
        except RepoPolicyError:
            continue
        listed.append(r)
    primary = next((r for r in listed if r != HAIKU), None) or (HAIKU if HAIKU in listed else None)
    escalation = HAIKU if primary not in (None, HAIKU) and HAIKU in listed else None
    return primary, escalation


class Action(StrEnum):
    ACT = "act"
    ESCALATED = "escalated"
    WAIT = "wait"
    DRIFT = "drift"  # a Jev answer refused for its version; recorded, never acted on


class Backend(Protocol):
    name: str

    def ask(
        self,
        q: Question,
        state: str,
        *,
        repo: str,
        job_id: str | None,
        private: bool,
        purpose: str = "decider",
    ) -> Answer: ...


@dataclass(frozen=True)
class Decision:
    answer: Answer
    action: Action


NO_ANSWER = "no-answer"


class Decider:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        backends: Mapping[str, Backend],
        *,
        breakers: Any = None,
        telemetry: Any = None,
        private: Callable[[str], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.backends = dict(backends)
        self.breakers = breakers
        self.telemetry = telemetry
        self.private = private  # the visibility source; None: vigild's (repos.py)

    # --- records ------------------------------------------------------------------

    def _record(self, a: Answer, action: Action, job_id: str | None, task_id: str | None) -> None:
        # Hosted non-Claude output (Jev) has no provenance value of its own; it
        # is tagged `claude`, the conservative tag the training exporter refuses.
        route = self.cfg.backends.decider_routes.get(a.backend)
        local = route is not None and route.locality == "local"
        prov = Provenance.LOCAL if local else Provenance.CLAUDE
        kind = a.kind or QUESTIONS_KIND.get(a.question, "")
        self.store.x(
            "INSERT INTO decisions (job_id, task_id, question, kind, answer, probability, backend,"
            " model_version, action, ts, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task_id,
                a.question,
                kind,
                str(a.value),
                a.probability,
                a.backend,
                a.model_version or "unknown",
                action.value,
                iso(),
                prov.value,
            ),
        )

    @contextmanager
    def _span(self, route: str, q: Question, repo: str, private: bool) -> Iterator[dict[str, Any]]:
        attrs: dict[str, Any] = {
            "vigil.decider.route": route,
            "vigil.decider.question": q.name,
            "vigil.repo": repo,
            "vigil.repo.visibility": "private" if private else "public",
            "vigil.decider.model_version": "unknown",
            "vigil.decider.kind": q.kind,
        }
        cm = self.telemetry.span_alone("vigil.decider", {}) if self.telemetry else nullcontext()
        with cm as span:
            yield attrs
            if span is not None:
                for k, v in attrs.items():
                    span.set_attribute(k, v)

    def _fallback_spend_today(self) -> float:
        day = utcnow().strftime("%Y-%m-%d")
        rows = self.store.q(
            "SELECT SUM(cost_usd) n FROM model_calls WHERE purpose=? AND ts LIKE ?",
            (FALLBACK, f"{day}%"),
        )
        return float(rows[0]["n"] or 0.0)

    def _drift(self, e: Any, escalation: str | None, job_id: str | None) -> None:
        """One inbox item per new (route, version), however many calls drift.
        The row and its page commit together: a crash between them leaves
        neither, and the next drifting call pages (review A9)."""
        where = "on Haiku fallback" if escalation else "waiting for the owner"
        title = (
            f"Jev version drift: {e.route} returned {e.version}, pinned {e.pin}; "
            f"decisions now {where}"
        )
        with self.store.tx() as c:
            seen = c.execute(
                "SELECT 1 FROM jev_drift WHERE route=? AND version=?", (e.route, e.version)
            ).fetchone()
            if seen:
                return
            item = self.store.create_inbox_item(
                "questions", "page", job_id, title,
                {"kind": "jev_drift", "route": e.route, "version": e.version, "pin": e.pin,
                 "decisions_now": where,
                 "fallback_spend_today_usd": round(self._fallback_spend_today(), 6),
                 "next": "a version bump is a PR, after the decider eval set is replayed",
                 "actions": ["dismiss"]},
            )  # fmt: skip
            c.execute(
                "INSERT INTO jev_drift (route, version, pin, item_id, first_seen)"
                " VALUES (?,?,?,?,?)",
                (e.route, e.version, e.pin, item.id, iso()),
            )

    def _credit(self, job_id: str | None, err: str, escalation: str | None) -> None:
        """Trip the credit breaker; it pages once, saying where decisions go."""
        from vigild.breakers import Kind

        if self.breakers is not None:
            where = "on Haiku fallback" if escalation else "waiting for the owner"
            self.breakers.trip(
                Kind.DECIDER_CREDIT, f"route:{CREDIT_ROUTE}",
                {"code": 2021, "error": err[:300], "decisions_now": where,
                 "fallback_spend_today_usd": round(self._fallback_spend_today(), 6),
                 "next": "add AI Gateway credit in Cloudflare, then reset this"},
                job_id=job_id,
                title=f"Cloudflare Jev credit is empty (2021); decisions now {where}",
            )  # fmt: skip

    # --- asking -------------------------------------------------------------------

    def _credit_off(self) -> bool:
        from vigild.breakers import Kind

        return self.breakers is not None and self.breakers.is_open(
            Kind.DECIDER_CREDIT, f"route:{CREDIT_ROUTE}"
        )

    def _ask(
        self,
        route: str,
        q: Question,
        state: str,
        repo: str,
        job_id: str | None,
        private: bool,
        purpose: str = DECIDER,
    ) -> Answer:
        """One route's answer. Raises DeciderError (and its kinds) on failure,
        after recording it."""
        with self._span(route, q, repo, private) as attrs:
            try:
                check_decider_route(self.cfg, repo, route, private=private)  # item 11
                a = self.backends[route].ask(
                    q, state, repo=repo, job_id=job_id, private=private, purpose=purpose
                )
            except (DeciderError, RepoPolicyError, GatewayError) as e:
                version = getattr(e, "version", None)
                if version:
                    attrs["vigil.decider.model_version"] = version
                attrs["vigil.decider.error"] = type(e).__name__
                self.store.add_event(
                    "decision",
                    {"route": route, "question": q.name, "error": str(e)},
                    Provenance.HUMAN,
                    job_id=job_id,
                )
                if isinstance(e, DeciderError):
                    raise
                raise DeciderError(str(e)) from e
            attrs["vigil.decider.model_version"] = a.model_version or "unknown"
            attrs["vigil.decider.confidence"] = a.probability
            return a

    def decide(
        self,
        repo: str,
        q: Question,
        state: str,
        *,
        job_id: str | None = None,
        task_id: str | None = None,
    ) -> Decision:
        from vigild.decider.backends.jev import JevCreditExhausted, JevVersionDrift

        # one lookup per decision, used by every call in it (review A10); fails closed
        private = live_private(repo, self.private)
        primary, escalation = decider_routes(self.cfg, repo, self.backends, private=private)
        if primary is None:
            return self._wait(q, job_id, task_id)
        if primary == CREDIT_ROUTE and self._credit_off():
            # the credit breaker is open: Haiku is this repo's fallback (A2)
            return self._fall_back(escalation, q, state, repo, job_id, task_id, private)
        try:
            a = self._ask(primary, q, state, repo, job_id, private)
        except JevCreditExhausted as e:
            self._credit(job_id, str(e), escalation)
            return self._fall_back(escalation, q, state, repo, job_id, task_id, private)
        except JevVersionDrift as e:
            if e.answer is not None:
                self._record(e.answer, Action.DRIFT, job_id, task_id)
            self._drift(e, escalation, job_id)
            return self._fall_back(escalation, q, state, repo, job_id, task_id, private)
        except DeciderError:
            return self._fall_back(escalation, q, state, repo, job_id, task_id, private)
        if a.probability >= ACT:
            self._record(a, Action.ACT, job_id, task_id)
            return Decision(a, Action.ACT)
        if escalation is not None and a.probability >= ESCALATE:
            self._record(a, Action.ESCALATED, job_id, task_id)
            return self._via_haiku(q, state, repo, job_id, task_id, private, DECIDER)
        self._record(a, Action.WAIT, job_id, task_id)
        return Decision(a, Action.WAIT)

    def _fall_back(
        self,
        escalation: str | None,
        q: Question,
        state: str,
        repo: str,
        job_id: str | None,
        task_id: str | None,
        private: bool,
    ) -> Decision:
        if escalation is None:
            return self._wait(q, job_id, task_id)
        return self._via_haiku(q, state, repo, job_id, task_id, private, FALLBACK)

    def _via_haiku(
        self,
        q: Question,
        state: str,
        repo: str,
        job_id: str | None,
        task_id: str | None,
        private: bool,
        purpose: str,
    ) -> Decision:
        try:
            h = self._ask(HAIKU, q, state, repo, job_id, private, purpose)
        except DeciderError:
            return self._wait(q, job_id, task_id)
        action = Action.ACT if h.probability >= ACT else Action.WAIT
        self._record(h, action, job_id, task_id)
        return Decision(h, action)

    def _wait(self, q: Question, job_id: str | None, task_id: str | None) -> Decision:
        a = Answer(q.name, None, 0.0, NO_ANSWER, None, q.kind)
        self.store.x(
            "INSERT INTO decisions (job_id, task_id, question, kind, answer, probability, backend,"
            " model_version, action, ts, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task_id,
                q.name,
                q.kind,
                "",
                0.0,
                NO_ANSWER,
                "unknown",
                Action.WAIT.value,
                iso(),
                Provenance.HUMAN.value,
            ),
        )
        return Decision(a, Action.WAIT)
