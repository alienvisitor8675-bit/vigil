"""Claude Haiku with structured outputs (`output_config.format`), called
through vigild's gateway so every call is metered and budgeted."""

from __future__ import annotations

import json
from typing import Any, Protocol

from vigild.decider.questions import Answer, DeciderError, Question
from vigild.packets.fence import UNTRUSTED_RULE, fence

PRESET = "claude-haiku"

SYSTEM = (
    "You answer one typed question about a software task, for an orchestrator. "
    "Reply only with the JSON the schema asks for: `answer` and `probability`, your "
    "calibrated probability (0 to 1) that the answer is right. " + UNTRUSTED_RULE
)


class ModelCaller(Protocol):
    """vigild's gateway, called in process (vigild/gateway/service.py)."""

    def call(
        self, *, repo: str, preset: str, body: dict[str, Any], purpose: str, job_id: str | None
    ) -> dict[str, Any]: ...


class HaikuBackend:
    name = "haiku"

    def __init__(self, caller: ModelCaller) -> None:
        self.caller = caller

    def body(self, q: Question, state: str) -> dict[str, Any]:
        options = f"\nOptions: {', '.join(q.options)}" if q.kind == "choice" else ""
        return {
            "max_tokens": 1024,
            "system": SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": f"{fence('task-state', state)}\n\nQuestion: {q.prompt}{options}",
                }
            ],
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": q.answer_schema()},
            },
        }

    def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
        reply = self.caller.call(
            repo=repo, preset=PRESET, body=self.body(q, state), purpose="decider", job_id=job_id
        )
        texts = [b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text"]
        try:
            data = json.loads(texts[-1])
        except (IndexError, json.JSONDecodeError) as e:
            raise DeciderError(f"haiku reply is not the schema's JSON: {texts[:1]}") from e
        return q.parse(
            data.get("answer"),
            data.get("probability"),
            backend=self.name,
            model_version=reply.get("model"),
        )
