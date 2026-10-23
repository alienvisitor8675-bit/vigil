"""TypeSafe Jev (B21): Cloudflare Workers AI (zero retention, unversioned
id) and OpenRouter's Decisions API (pinned `typesafe/jev-1.13`, ZDR).

Coded against synthetic fixtures shaped from the published docs
(tests/decider/fixtures); it goes live when the owner provides the
Cloudflare token, and the fixtures are then replaced with recorded ones.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import httpx2

from vigild.decider.questions import Answer, DeciderError, Question

CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
OR_URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT = 20.0


def _question(q: Question) -> dict[str, Any]:
    if q.kind == "choice":
        options = list(q.options)
    elif q.kind == "score":
        options = ["1", "2", "3", "4", "5"]
    else:
        options = ["yes", "no"]
    return {"id": q.name, "type": "choice", "prompt": q.prompt, "options": options}


def _parse(q: Question, payload: dict[str, Any], backend: str) -> tuple[Answer, str | None]:
    result = payload.get("result", payload)
    try:
        ans = next(a for a in result["answers"] if a.get("id") == q.name)
        choice = ans["choice"]
        prob = ans.get("probability", ans.get("probabilities", {}).get(choice))
    except (KeyError, StopIteration, TypeError) as e:
        raise DeciderError(f"{backend}: unexpected response shape") from e
    version = result.get("model")
    return q.parse(choice, prob, backend=backend, model_version=version), version


class Meter(Protocol):
    """The gateway's shared metering path (review round 1, item 11): it checks
    the repo lists the route, reserves, calls, records the call, settles."""

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
    ) -> dict[str, Any]: ...


class _Jev:
    name = "jev"
    provider = ""
    model = ""
    version_changed = False

    def __init__(self, client: httpx2.Client, known_version: str | None, *, meter: Meter) -> None:
        self.client = client
        self.known_version = known_version
        self.meter = meter  # required: no Jev call goes unmetered (review round 2, N9)

    def _call(
        self, repo: str, job_id: str | None, run: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        return self.meter(
            repo=repo,
            route=self.name,
            provider=self.provider,
            model=self.model,
            purpose="decider",
            job_id=job_id,
            run=run,
        )

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.client.post(url, headers=headers, json=body, timeout=TIMEOUT)
        except httpx2.HTTPError as e:
            raise DeciderError(f"{self.name}: {type(e).__name__}") from e
        if r.status_code != 200:
            raise DeciderError(f"{self.name}: HTTP {r.status_code}")
        data: dict[str, Any] = r.json()
        return data

    def _finish(self, q: Question, payload: dict[str, Any]) -> Answer:
        answer, version = _parse(q, payload, self.name)
        if self.known_version and version and version != self.known_version:
            self.version_changed = True  # flagged on the decider page (phase 3)
        return answer


class JevCloudflare(_Jev):
    name = "jev-cloudflare"
    provider = "cloudflare"
    model = "typesafe/jev"

    def __init__(
        self,
        client: httpx2.Client,
        *,
        account_id: str,
        token: str,
        known_version: str | None = None,
        meter: Meter,
    ) -> None:
        super().__init__(client, known_version, meter=meter)
        self.account_id = account_id
        self.token = token

    def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
        # Cloudflare caps context at 32k: duplicate_of gets titles, not bodies (B21).
        body = {
            "model": "typesafe/jev",
            "input": {"context": state[:96_000], "questions": [_question(q)]},
        }
        url = CF_URL.format(account=self.account_id)
        payload = self._call(
            repo, job_id, lambda: self._post(url, {"Authorization": f"Bearer {self.token}"}, body)
        )
        if payload.get("success") is False:
            raise DeciderError("jev-cloudflare: success=false")
        return self._finish(q, payload)


class JevOpenRouter(_Jev):
    name = "jev-openrouter"
    provider = "openrouter"
    model = "typesafe/jev-1.13"

    def __init__(
        self,
        client: httpx2.Client,
        *,
        token: str,
        known_version: str | None = "jev-1.13",
        meter: Meter,
    ) -> None:
        super().__init__(client, known_version, meter=meter)
        self.token = token

    def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
        body = {
            "model": "typesafe/jev-1.13",
            "provider": {"zdr": True, "data_collection": "deny"},
            "input": {"context": state, "questions": [_question(q)]},
        }
        payload = self._call(
            repo,
            job_id,
            lambda: self._post(OR_URL, {"Authorization": f"Bearer {self.token}"}, body),
        )
        return self._finish(q, payload)
