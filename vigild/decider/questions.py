"""Decider questions: one schema shared by every backend, so swapping
backends is config. The decider never writes code or prose."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

MAX_OPTIONS = 255

# TypeSafe Jev's three question types (owner, 2026-10-23): a choice, a 1-5
# score, and a noul, a single float from 0 to 1 (how likely "yes" is). A
# noul's float is its own probability: act at >= 0.85, escalate at >= 0.6.
Kind = Literal["choice", "score", "noul"]


class DeciderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answer:
    question: str
    value: Any
    probability: float
    backend: str
    model_version: str | None = None


@dataclass(frozen=True)
class Question:
    name: str
    kind: Kind
    prompt: str
    options: list[str]

    def with_options(self, options: list[str]) -> Question:
        opts = ["none", *[o for o in options if o != "none"]][:MAX_OPTIONS]
        return replace(self, options=opts)

    def answer_schema(self) -> dict[str, Any]:
        """The JSON schema a structured-output backend must fill."""
        answer: dict[str, Any]
        if self.kind == "choice":
            answer = {"type": "string", "enum": list(self.options)}
        elif self.kind == "score":
            answer = {"type": "integer", "enum": [1, 2, 3, 4, 5]}
        else:
            answer = {"type": "number", "minimum": 0, "maximum": 1}
        return {
            "type": "object",
            "properties": {"answer": answer, "probability": {"type": "number"}},
            "required": ["answer", "probability"],
            "additionalProperties": False,
        }

    def parse(
        self, raw: Any, probability: Any, *, backend: str, model_version: str | None = None
    ) -> Answer:
        if self.kind == "noul":
            probability = raw  # a noul is its own probability; any other number is ignored
        try:
            p = float(probability)
        except (TypeError, ValueError) as e:
            raise DeciderError(f"{self.name}: probability {probability!r} is not a number") from e
        if not 0.0 <= p <= 1.0:
            raise DeciderError(f"{self.name}: probability {p} is outside 0-1")
        value: Any
        if self.kind == "choice":
            if str(raw) not in self.options:
                raise DeciderError(f"{self.name}: {raw!r} is not one of {self.options[:8]}")
            value = str(raw)
        elif self.kind == "score":
            try:
                value = int(raw)
            except (TypeError, ValueError) as e:
                raise DeciderError(f"{self.name}: {raw!r} is not a score") from e
            if not 1 <= value <= 5:
                raise DeciderError(f"{self.name}: score {value} is outside 1-5")
        else:
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise DeciderError(f"{self.name}: noul {raw!r} is not a number")
            value = float(raw)
            if not 0.0 <= value <= 1.0:
                raise DeciderError(f"{self.name}: noul {value} is outside 0-1")
            p = value  # a noul is its own probability (owner, 2026-10-23)
        return Answer(self.name, value, p, backend, model_version)


QUESTIONS: dict[str, Question] = {
    q.name: q
    for q in (
        Question(
            "tier",
            "choice",
            "Which routing tier fits this task?",
            ["claude-opus", "claude-sonnet", "local", "local-fast"],
        ),
        Question("size", "choice", "How big is this task?", ["XS", "S", "M", "L"]),
        Question("value", "score", "How valuable is doing this (1 low, 5 high)?", []),
        Question("risk", "score", "How risky is this change (1 low, 5 high)?", []),
        Question(
            "touches_money_path",
            "noul",
            "Does this touch billing, payments, auth or consent code?",
            [],
        ),
        Question("worth_proposing", "noul", "Is this finding worth proposing as a task?", []),
        Question(
            "needs_escalation",
            "noul",
            "Do these review findings need escalation to a stronger model?",
            [],
        ),
        Question(
            "duplicate_of",
            "choice",
            "Is this a duplicate of one of these open issues? Answer its id, or none.",
            ["none"],
        ),
    )
}
