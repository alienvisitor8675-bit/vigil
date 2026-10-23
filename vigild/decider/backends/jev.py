"""TypeSafe Jev (B21), on two routes, both checked live (owner, 2026-10-23;
recorded 2026-10-22, tests/decider/fixtures):

- **Cloudflare Workers AI** (zero retention), the route for private repos:
  `POST /client/v4/accounts/{id}/ai/run`, body `{"model": "typesafe/jev",
  "input": {"state", "questions": {<name>: {"type", "instructions",
  "criteria"}}}}`; the answers nest at `result.result.answers`, the run's
  state at `result.state` (anything but `Completed` is an error), the
  version at `result.result.model`, the usage at `result.result.usage`.
  `typesafe/jev` is a floating alias: no pinned id exists there (neither
  `typesafe/jev-1.13.0` nor `typesafe/jev-1.13` is found, and the account's
  model search lists no Jev), so the version pin below holds it.
- **OpenRouter's Decisions API** (ZDR), the route for public repos: `POST
  /api/alpha/decisions`, body `{"model": "typesafe/jev-1.13", "provider",
  "state", "questions"}` at the top level; the answers, `model` (the version)
  and `usage` at the top level of the response.

Each answer is `{type, <type>: value, probabilities, confidence}`; the value
sits under the key its type names (choice, score, noul). A score's value is
the likeliest of its five anchors; a noul is a float 0-1, its own probability.

**Version pin** (owner, 2026-10-23): each route's `version_pin`
(configs/backends.yaml) is the exact string its responses report for 1.13.
Another version, or none, raises `JevVersionDrift`: the decision falls back
down the chain, and the decider files one inbox item per new (route,
version). A bump happens only by PR, after the decider eval set is replayed.

**Cloudflare 2021** ("Insufficient balance": the AI Gateway credit is empty)
raises `JevCreditExhausted`; the decider trips the credit breaker and pages.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import httpx2

from vigild.decider.questions import Answer, DeciderError, Question

CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
OR_URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT = 20.0
SCORE_ANCHORS = ["1 (lowest)", "2", "3", "4", "5 (highest)"]
CREDIT_EMPTY = 2021  # Cloudflare: "Insufficient balance" (AI Gateway credit)
UNKNOWN = "unknown"


class JevCreditExhausted(DeciderError):
    """Cloudflare's 2021: the AI Gateway credit is empty."""


class JevVersionDrift(DeciderError):
    """A response reported a version other than the route's pin (or none)."""

    def __init__(self, route: str, version: str, pin: str, answer: Answer | None) -> None:
        super().__init__(f"{route}: Jev reported {version}, pinned {pin}")
        self.route, self.version, self.pin, self.answer = route, version, pin, answer


def question(q: Question) -> dict[str, Any]:
    """A question as Jev takes it, on both routes."""
    criteria: Any
    if q.kind == "choice":
        criteria = {o: o for o in q.options}
    elif q.kind == "score":
        criteria = list(SCORE_ANCHORS)
    else:
        criteria = {"true": "yes", "false": "no"}
    return {"type": q.kind, "instructions": q.prompt, "criteria": criteria}


def parse_answer(q: Question, answers: Any, backend: str, version: str) -> Answer:
    """One question's answer, read as `answer[answer["type"]]`."""
    try:
        ans = answers[q.name]
        kind = ans["type"]
        raw = ans[kind]
    except (KeyError, TypeError) as e:
        raise DeciderError(f"{backend}: no answer for {q.name}") from e
    if kind != q.kind:
        raise DeciderError(f"{backend}: {q.name} came back as a {kind}, asked as a {q.kind}")
    probs = ans.get("probabilities") or {}
    if not isinstance(probs, dict):  # a list or a string: a DeciderError, so it falls back
        raise DeciderError(f"{backend}: {q.name} probabilities aren't a mapping")
    if kind == "choice":
        return q.parse(raw, probs.get(raw), backend=backend, model_version=version,
                       probabilities=probs)  # fmt: skip
    if kind == "score":
        # Jev's `score` is on its anchors' 0-4 index (the legend): +1 puts it on 1-5.
        # Its probability is the likeliest anchor's.
        try:
            best = max(probs, key=lambda k: float(probs[k]))
            return q.parse(float(raw) + 1, probs[best], backend=backend, model_version=version)
        except (ValueError, TypeError) as e:
            raise DeciderError(f"{backend}: unreadable score for {q.name}") from e
    # noul: the live answer is one float, P(true); `probabilities` with true and
    # false keys wins if a response ever carries it
    return q.parse(raw, None, backend=backend, model_version=version,
                   probabilities=probs or None)  # fmt: skip


class Meter(Protocol):
    """The gateway's shared metering path (review round 1, item 11): it checks
    the repo may use the route (by its live visibility), reserves, calls,
    records the call, settles."""

    def __call__(
        self,
        *,
        repo: str,
        route: str,
        provider: str,
        model: str,
        purpose: str,
        job_id: str | None,
        run: Callable[[], dict[str, Any]],
        private: bool | None = None,
    ) -> dict[str, Any]: ...


class _Jev:
    name = "jev"
    provider = ""
    model = ""

    def __init__(self, client: httpx2.Client, version_pin: str, *, meter: Meter) -> None:
        if not version_pin:
            raise ValueError(f"{self.name}: a Jev route needs its version_pin (review A8)")
        self.client = client
        self.version_pin = version_pin
        self.meter = meter  # required: no Jev call goes unmetered (review round 2, N9)

    def _call(
        self, repo: str, job_id: str | None, run: Callable[[], dict[str, Any]], private: bool
    ) -> dict[str, Any]:
        return self.meter(
            repo=repo,
            route=self.name,
            provider=self.provider,
            model=self.model,
            purpose="decider",
            job_id=job_id,
            run=run,
            private=private,
        )

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """The response body, read whatever the status (Cloudflare's error
        codes come on non-200s too)."""
        try:
            r = self.client.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except httpx2.HTTPError as e:
            raise DeciderError(f"{self.name}: {type(e).__name__}") from e
        try:
            data: Any = r.json()
        except ValueError:
            data = None
        codes = [
            e.get("code")
            for e in ((data or {}).get("errors") or [] if isinstance(data, dict) else [])
            if isinstance(e, dict)
        ]
        if CREDIT_EMPTY in codes:
            raise JevCreditExhausted(f"{self.name}: 2021 Insufficient balance (AI Gateway credit)")
        if r.status_code != 200 or not isinstance(data, dict):
            raise DeciderError(f"{self.name}: HTTP {r.status_code} {codes or ''}".strip())
        return data

    def _check_version(self, answer: Answer) -> Answer:
        version = answer.model_version or UNKNOWN
        if version != self.version_pin:
            raise JevVersionDrift(self.name, version, self.version_pin, answer)
        return answer


class JevCloudflare(_Jev):
    name = "jev-cloudflare"
    provider = "cloudflare"
    model = "typesafe/jev"  # a floating alias; the version pin holds it to 1.13

    def __init__(
        self,
        client: httpx2.Client,
        *,
        account_id: str,
        token: str,
        version_pin: str,
        meter: Meter,
    ) -> None:
        super().__init__(client, version_pin, meter=meter)
        self.account_id = account_id
        self.token = token

    def ask(
        self,
        q: Question,
        state: str,
        *,
        repo: str,
        job_id: str | None,
        private: bool,
        purpose: str = "decider",
    ) -> Answer:
        # Cloudflare caps context at 32k: duplicate_of gets titles, not bodies (B21).
        body = {
            "model": self.model,
            "input": {"state": state[:96_000], "questions": {q.name: question(q)}},
        }
        url = CF_URL.format(account=self.account_id)

        def run() -> dict[str, Any]:
            payload = self._post(url, {"Authorization": f"Bearer {self.token}"}, body)
            inner = (payload.get("result") or {}).get("result") or {}
            return {**payload, "usage": inner.get("usage") or {}}  # for the meter

        payload = self._call(repo, job_id, run, private)
        outer = payload.get("result") or {}
        if outer.get("state") != "Completed":
            raise DeciderError(f"jev-cloudflare: the run's state is {outer.get('state')!r}")
        inner = outer.get("result") or {}
        version = str(inner.get("model") or UNKNOWN)
        return self._check_version(parse_answer(q, inner.get("answers"), self.name, version))


class JevOpenRouter(_Jev):
    name = "jev-openrouter"
    provider = "openrouter"
    model = "typesafe/jev-1.13"  # pinned; a bump is a PR after the eval set is replayed

    def __init__(
        self,
        client: httpx2.Client,
        *,
        token: str,
        version_pin: str,
        meter: Meter,
    ) -> None:
        super().__init__(client, version_pin, meter=meter)
        self.token = token

    def ask(
        self,
        q: Question,
        state: str,
        *,
        repo: str,
        job_id: str | None,
        private: bool,
        purpose: str = "decider",
    ) -> Answer:
        body = {
            "model": self.model,
            "provider": {"zdr": True, "data_collection": "deny"},
            "state": state,
            "questions": {q.name: question(q)},
        }
        payload = self._call(
            repo,
            job_id,
            lambda: self._post(OR_URL, {"Authorization": f"Bearer {self.token}"}, body),
            private,
        )
        version = str(payload.get("model") or UNKNOWN)
        return self._check_version(parse_answer(q, payload.get("answers"), self.name, version))
