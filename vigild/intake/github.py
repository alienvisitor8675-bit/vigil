"""GitHub label intake (plan 2a, item 2): an open issue labelled
`vigil:ready`, in a repo with `intake: true`, becomes a `spec_pending` job,
exactly once. Polled; a webhook comes later.

- **Who can start work:** only people who can label issues in the repo, so a
  labelled issue is the owner's say-so. The issue's text is untrusted all the
  same: it reaches the coord only fenced (`issue.json`, provenance human).
- **Exactly once:** vigild claims `(repo, number)` in `intake_seen` before it
  creates the job, in one transaction; a second poll, or a second vigild,
  finds the claim. An issue the intake gate refuses is claimed too, with the
  reason, and not retried.
- **Token:** `issues: read`, one repo, one hour, revoked after each poll.
- **Scores:** `value:N` and `risk:N` labels. A score no label gives is asked
  of the decider (Jev on OpenRouter for a public repo, Haiku the escalation;
  `vigild/decider/wiring.py` builds it): the fleet's first decider call. An
  answer it acts on is used; one it waits on leaves the score at 3, and the
  owner sees both in the spec's approval item.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from vigild.config import Config
from vigild.contract.models import Source
from vigild.db import Store, iso
from vigild.provenance import Provenance

log = logging.getLogger("vigild.intake")
LABEL = "vigil:ready"
SCORE = re.compile(r"^(value|risk):([1-5])$")
MAX_BODY = 20_000


class Issues(Protocol):
    def labelled(self, repo: str, label: str) -> list[dict[str, Any]]: ...


class Submitter(Protocol):
    def submit_request(self, req: Any, *, issue: dict[str, Any] | None = None) -> Any: ...


class RestIssues:
    """GitHub's REST API, with an `issues: read` token per repo and poll."""

    def __init__(self, app: Any, api: str = "https://api.github.com") -> None:
        self.app = app
        self.api = api.rstrip("/")

    def labelled(self, repo: str, label: str) -> list[dict[str, Any]]:
        with self.app.lease(repo, {"issues": "read"}) as tok:
            r = self.app.http.get(
                f"{self.api}/repos/{repo}/issues",
                params={"labels": label, "state": "open", "per_page": 50},
                headers={
                    "Authorization": f"Bearer {tok.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=20.0,
            )
        if r.status_code != 200:
            raise RuntimeError(f"listing {repo} issues failed (HTTP {r.status_code})")
        out: list[dict[str, Any]] = r.json()
        return out


class Decides(Protocol):
    def decide(self, repo: str, q: Any, state: str, *, job_id: str | None = None) -> Any: ...


class LabelIntake:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        jobs: Submitter,
        issues: Issues,
        decider: Decides | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.jobs = jobs
        self.issues = issues
        self.decider = decider

    def _score(self, repo: str, name: str, text: str) -> tuple[int, dict[str, Any] | None]:
        """A score the labels didn't give: the decider's when it acts, else 3."""
        if self.decider is None:
            return 3, None
        from vigild.decider.policy import Action
        from vigild.decider.questions import QUESTIONS

        d = self.decider.decide(repo, QUESTIONS[name], text)
        v = d.answer.value  # a number on the 1-5 scale (owner ruling 5)
        used = d.action is Action.ACT and isinstance(v, int | float) and not isinstance(v, bool)
        record = {"value": v, "probability": d.answer.probability,
                  "backend": d.answer.backend, "action": d.action.value}  # fmt: skip
        return (min(5, max(1, round(float(v)))) if used else 3), record

    def _claim(self, repo: str, number: int) -> bool:
        with self.store.tx() as c:
            seen = c.execute(
                "SELECT 1 FROM intake_seen WHERE repo=? AND number=?", (repo, number)
            ).fetchone()
            if seen:
                return False
            c.execute(
                "INSERT INTO intake_seen (repo, number, seen_at) VALUES (?,?,?)",
                (repo, number, iso()),
            )
        return True

    def poll(self) -> list[str]:
        """One pass over every repo that takes label intake; the new job ids."""
        from vigild.jobs.service import JobRequest, SubmitError

        made: list[str] = []
        for repo, pol in self.cfg.repos.repos.items():
            if not pol.intake:
                continue
            try:
                issues = self.issues.labelled(repo, LABEL)
            except Exception as e:  # GitHub down: try again next poll
                log.warning("intake: %s: %s", repo, e)
                continue
            for issue in issues:
                if "pull_request" in issue or not isinstance(issue.get("number"), int):
                    continue
                number = int(issue["number"])
                if not self._claim(repo, number):
                    continue
                labels = [str(lb.get("name", "")) for lb in issue.get("labels") or []]
                scores = {m.group(1): int(m.group(2)) for m in map(SCORE.match, labels) if m}
                title = str(issue.get("title") or f"issue #{number}")[:200]
                body = str(issue.get("body") or "")[:MAX_BODY]
                decided: dict[str, Any] = {}
                for name in ("value", "risk"):
                    if name not in scores:
                        scores[name], rec = self._score(repo, name, f"{title}\n\n{body}")
                        if rec is not None:
                            decided[name] = rec
                req = JobRequest(
                    repo=repo,
                    title=f"#{number}: {title}",
                    request=(
                        f"GitHub issue {repo}#{number}, labelled {LABEL}: {title}. "
                        "The issue's text is in the packet's untrusted section."
                    ),
                    why=f"{repo}#{number} is labelled {LABEL}",
                    value=scores.get("value", 3),
                    risk=scores.get("risk", 3),
                    source=Source.HUMAN,
                    labels=[lb for lb in labels if lb != LABEL and not SCORE.match(lb)],
                )
                detail = {
                    "number": number,
                    "url": issue.get("html_url"),
                    "title": title,
                    "body": body,
                    "decider": decided,
                }
                try:
                    job = self.jobs.submit_request(req, issue=detail)
                except SubmitError as e:
                    self._record(repo, number, None, {"refused": e.reasons})
                    continue
                self._record(repo, number, job.id, {"job_id": job.id})
                made.append(job.id)
        return made

    def _record(self, repo: str, number: int, job_id: str | None, data: dict[str, Any]) -> None:
        self.store.x(
            "UPDATE intake_seen SET job_id=? WHERE repo=? AND number=?", (job_id, repo, number)
        )
        self.store.add_event(
            "intake", {"repo": repo, "issue": number, **data}, Provenance.HUMAN, job_id=job_id
        )


def issue_text(store: Store, job_id: str) -> str | None:
    """The labelled issue a job came from, for the coord's fenced section."""
    art = store.get_artifact(job_id, "issue.json")
    if art is None:
        return None
    doc = json.loads(art.content)
    return f"{doc.get('url')}\n\n# {doc.get('title')}\n\n{doc.get('body')}"


def every(seconds: int, fn: Callable[[], Any], stop: Any) -> None:
    """Run `fn` now and every `seconds` until `stop` (a threading.Event) is set;
    an exception is logged and the loop goes on."""
    while not stop.is_set():
        try:
            fn()
        except Exception:
            log.exception("poll failed")
        stop.wait(seconds)
