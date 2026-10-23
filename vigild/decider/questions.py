"""Decider questions: one schema shared by every backend, so swapping
backends is config. The decider never writes code or prose."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

MAX_OPTIONS = 255

# Three question types, three answer shapes (owner, 2026-10-23, round 2):
# - choice: the chosen option, its probability, and every option's;
# - score: a number on the 1-5 scale, with its probability;
# - noul: yes/no (true/false) with P(true). The thresholds (act >= 0.85,
#   escalate >= 0.6) apply to P(true), so a confident "no" waits.
Kind = Literal["choice", "score", "noul"]


class DeciderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answer:
    """`value`: the option (choice), a number on 1-5 (score), or a bool (noul).
    `probability`: what the thresholds read; for a noul, P(true)."""

    question: str
    value: Any
    probability: float
    backend: str
    model_version: str | None = None
    kind: str = ""
    probabilities: dict[str, float] | None = None


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
        else:  # noul: the answer is P(true), the probability it's "yes"
            answer = {"type": "number", "minimum": 0, "maximum": 1,
                      "description": "the probability that the answer is yes (true)"}  # fmt: skip
        return {
            "type": "object",
            "properties": {"answer": answer, "probability": {"type": "number"}},
            "required": ["answer", "probability"],
            "additionalProperties": False,
        }

    def parse(
        self,
        raw: Any,
        probability: Any,
        *,
        backend: str,
        model_version: str | None = None,
        probabilities: dict[str, Any] | None = None,
    ) -> Answer:
        """One answer in its type's shape. A noul's `raw` is P(true), unless
        `probabilities` names true and false; `probability` is then ignored."""
        if self.kind == "noul":
            return self._noul(raw, probabilities, backend, model_version)
        p = _probability(self.name, probability)
        value: Any
        if self.kind == "choice":
            if str(raw) not in self.options:
                raise DeciderError(f"{self.name}: {raw!r} is not one of {self.options[:8]}")
            value = str(raw)
        else:
            if isinstance(raw, bool):
                raise DeciderError(f"{self.name}: {raw!r} is not a score")
            try:
                value = float(raw)
            except (TypeError, ValueError) as e:
                raise DeciderError(f"{self.name}: {raw!r} is not a score") from e
            if not 1.0 <= value <= 5.0:
                raise DeciderError(f"{self.name}: score {value} is outside 1-5")
        try:
            probs = {str(k): float(v) for k, v in probabilities.items()} if probabilities else None
        except (TypeError, ValueError) as e:  # a DeciderError, so the decision falls back
            raise DeciderError(
                f"{self.name}: probabilities {probabilities!r} aren't numbers"
            ) from e
        return Answer(self.name, value, p, backend, model_version, self.kind, probs)

    def _noul(
        self,
        raw: Any,
        probabilities: dict[str, Any] | None,
        backend: str,
        model_version: str | None,
    ) -> Answer:
        given = (probabilities or {}).get("true")
        source = given if given is not None else raw
        if isinstance(source, bool) or not isinstance(source, int | float):
            raise DeciderError(f"{self.name}: noul {source!r} is not a number")
        p_true = _probability(self.name, source)
        return Answer(self.name, p_true >= 0.5, p_true, backend, model_version, "noul",
                      {"true": p_true, "false": round(1.0 - p_true, 6)})  # fmt: skip


def _probability(name: str, probability: Any) -> float:
    try:
        p = float(probability)
    except (TypeError, ValueError) as e:
        raise DeciderError(f"{name}: probability {probability!r} is not a number") from e
    if not 0.0 <= p <= 1.0:
        raise DeciderError(f"{name}: probability {p} is outside 0-1")
    return p


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
