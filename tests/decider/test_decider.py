"""The decider: typed questions in, typed answers with probabilities out."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest

from vigild.config import Config
from vigild.db import Store
from vigild.decider.backends.haiku import HaikuBackend
from vigild.decider.backends.jev import JevCloudflare, JevOpenRouter
from vigild.decider.policy import Action, Decider
from vigild.decider.questions import QUESTIONS, Answer, DeciderError, Question

FIX = Path(__file__).parent / "fixtures"
PA = "thomasbardhi01/parkagent"


def passthrough(**kw: Any) -> dict[str, Any]:
    """A meter that only runs the call (the real one is Gateway.metered)."""
    return kw["run"]()  # type: ignore[no-any-return]


def test_launch_question_set() -> None:
    assert set(QUESTIONS) == {
        "tier",
        "size",
        "value",
        "risk",
        "touches_money_path",
        "worth_proposing",
        "needs_escalation",
        "duplicate_of",
    }
    assert QUESTIONS["size"].options == ["XS", "S", "M", "L"]
    assert QUESTIONS["value"].kind == "score"
    assert QUESTIONS["touches_money_path"].kind == "bool"


def test_duplicate_of_is_capped_at_255_options() -> None:
    q = QUESTIONS["duplicate_of"].with_options([str(i) for i in range(400)])
    assert len(q.options) == 255 and q.options[0] == "none"


def test_answer_must_be_one_of_the_options() -> None:
    q = QUESTIONS["size"]
    with pytest.raises(DeciderError):
        q.parse("XXL", 0.9, backend="x")
    assert q.parse("S", 0.9, backend="x").value == "S"
    assert QUESTIONS["value"].parse("4", 0.8, backend="x").value == 4
    with pytest.raises(DeciderError):
        QUESTIONS["value"].parse(6, 0.8, backend="x")
    assert QUESTIONS["touches_money_path"].parse("yes", 0.9, backend="x").value is True


@dataclass
class Fixed:
    name: str
    probability: float
    value: Any = "S"
    calls: list[str] = field(default_factory=list)

    def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
        self.calls.append(q.name)
        return Answer(q.name, self.value, self.probability, self.name, "v1")


def _decider(cfg: Config, store: Store, primary: Fixed, haiku: Fixed) -> Decider:
    return Decider(cfg, store, {primary.name: primary, "haiku": haiku})


def test_confident_primary_acts(cfg: Config, store: Store) -> None:
    jev, haiku = Fixed("jev-cloudflare", 0.9), Fixed("haiku", 0.99)
    d = _decider(cfg, store, jev, haiku).decide(PA, QUESTIONS["size"], "issue text")
    assert d.action is Action.ACT and d.answer.backend == "jev-cloudflare"
    assert haiku.calls == []


def test_middle_confidence_escalates_to_haiku(cfg: Config, store: Store) -> None:
    jev, haiku = Fixed("jev-cloudflare", 0.7), Fixed("haiku", 0.9, "M")
    d = _decider(cfg, store, jev, haiku).decide(PA, QUESTIONS["size"], "issue text")
    assert d.action is Action.ACT and d.answer.backend == "haiku" and d.answer.value == "M"
    rows = store.q("SELECT backend, action FROM decisions ORDER BY id")
    assert [(r["backend"], r["action"]) for r in rows] == [
        ("jev-cloudflare", "escalated"),
        ("haiku", "act"),
    ]


def test_unsure_haiku_waits_for_the_owner(cfg: Config, store: Store) -> None:
    jev, haiku = Fixed("jev-cloudflare", 0.7), Fixed("haiku", 0.7)
    d = _decider(cfg, store, jev, haiku).decide(PA, QUESTIONS["size"], "x")
    assert d.action is Action.WAIT


def test_low_confidence_waits_without_escalating(cfg: Config, store: Store) -> None:
    jev, haiku = Fixed("jev-cloudflare", 0.5), Fixed("haiku", 0.99)
    d = _decider(cfg, store, jev, haiku).decide(PA, QUESTIONS["size"], "x")
    assert d.action is Action.WAIT and haiku.calls == []


def test_haiku_alone_when_jev_is_unavailable(cfg: Config, store: Store) -> None:
    haiku = Fixed("haiku", 0.9)
    d = Decider(cfg, store, {"haiku": haiku}).decide(PA, QUESTIONS["size"], "x")
    assert d.action is Action.ACT and d.answer.backend == "haiku"


def test_a_route_the_repo_does_not_list_is_never_called(cfg: Config, store: Store) -> None:
    rogue, haiku = Fixed("jev-openrouter", 0.99), Fixed("haiku", 0.9)
    d = Decider(cfg, store, {"jev-openrouter": rogue, "haiku": haiku}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert rogue.calls == [] and d.answer.backend == "haiku"


def test_backend_error_means_wait(cfg: Config, store: Store) -> None:
    class Broken(Fixed):
        def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
            raise DeciderError("boom")

    d = Decider(cfg, store, {"haiku": Broken("haiku", 0)}).decide(PA, QUESTIONS["size"], "x")
    assert d.action is Action.WAIT


# --- backends ----------------------------------------------------------------------


@dataclass
class FakeCaller:
    reply: dict[str, Any]
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def call(
        self, *, repo: str, preset: str, body: dict[str, Any], purpose: str, job_id: str | None
    ) -> dict[str, Any]:
        self.bodies.append({"repo": repo, "preset": preset, "body": body, "purpose": purpose})
        return self.reply


def _haiku_reply(answer: Any, p: float) -> dict[str, Any]:
    return {
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps({"answer": answer, "probability": p})}],
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }


def test_haiku_uses_structured_outputs_through_the_gateway() -> None:
    caller = FakeCaller(_haiku_reply("M", 0.88))
    a = HaikuBackend(caller).ask(QUESTIONS["size"], "Add three tests", repo=PA, job_id=None)
    assert (a.value, a.probability, a.backend) == ("M", 0.88, "haiku")
    sent = caller.bodies[0]
    assert sent["preset"] == "claude-haiku" and sent["purpose"] == "decider"
    fmt = sent["body"]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["answer"]["enum"] == ["XS", "S", "M", "L"]
    assert "temperature" not in sent["body"]
    # untrusted state is fenced, never mixed into the instructions
    assert "Add three tests" not in sent["body"]["system"]


def test_haiku_reply_outside_the_options_is_an_error() -> None:
    with pytest.raises(DeciderError):
        HaikuBackend(FakeCaller(_haiku_reply("XXL", 0.9))).ask(
            QUESTIONS["size"], "x", repo=PA, job_id=None
        )


def test_jev_cloudflare_against_fixture() -> None:
    fixture = json.loads((FIX / "jev_cloudflare_choice.json").read_text())
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json=fixture)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    jev = JevCloudflare(
        client, account_id="acct", token="t0k", known_version="jev-1.13.0", meter=passthrough
    )
    a = jev.ask(QUESTIONS["size"], "Add three tests", repo=PA, job_id=None)
    assert (a.value, a.probability, a.model_version) == ("S", 0.91, "jev-1.13.0")
    req = seen[0]
    assert req.url.path == "/client/v4/accounts/acct/ai/run"
    assert req.headers["authorization"] == "Bearer t0k"
    body = json.loads(req.content)
    assert body["model"] == "typesafe/jev"
    assert body["input"]["questions"][0]["options"] == ["XS", "S", "M", "L"]
    assert not jev.version_changed


def test_jev_flags_a_model_version_change() -> None:
    fixture = json.loads((FIX / "jev_cloudflare_choice.json").read_text())
    fixture["result"]["model"] = "jev-1.14.0"
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=fixture))
    )
    jev = JevCloudflare(
        client, account_id="a", token="t", known_version="jev-1.13.0", meter=passthrough
    )
    jev.ask(QUESTIONS["size"], "x", repo=PA, job_id=None)
    assert jev.version_changed


def test_jev_http_error_is_a_decider_error() -> None:
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(500, text="no"))
    )
    with pytest.raises(DeciderError):
        JevCloudflare(client, account_id="a", token="t", meter=passthrough).ask(
            QUESTIONS["size"], "x", repo=PA, job_id=None
        )


def test_jev_openrouter_pins_the_model_and_zdr() -> None:
    fixture = json.loads((FIX / "jev_cloudflare_choice.json").read_text())
    fixture["result"]["model"] = "jev-1.13"
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json=fixture)

    jev = JevOpenRouter(
        httpx2.Client(transport=httpx2.MockTransport(handler)), token="t", meter=passthrough
    )
    assert jev.ask(QUESTIONS["size"], "x", repo=PA, job_id=None).value == "S"
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/alpha/decisions"
    assert body["model"] == "typesafe/jev-1.13" and body["provider"]["zdr"] is True


# --- review round 1, item 11: Jev is metered and budgeted like every other call ----------


def test_jev_calls_go_through_the_gateways_meter(cfg: Config, store: Store) -> None:
    from vigild.budget import Budget
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway

    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({}))
    fixture = json.loads((FIX / "jev_cloudflare_choice.json").read_text())
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=fixture))
    )
    jev = JevCloudflare(client, account_id="a", token="t", meter=gw.metered)
    assert jev.ask(QUESTIONS["size"], "x", repo=PA, job_id=None).value == "S"
    row = store.q("SELECT provider, model, tokens_in, cost_usd, purpose FROM model_calls")[0]
    assert (row["provider"], row["model"], row["tokens_in"]) == ("cloudflare", "typesafe/jev", 412)
    assert row["cost_usd"] == pytest.approx(412 * 0.042 / 1e6)
    assert store.q("SELECT COUNT(*) n FROM ledger")[0]["n"] == 1


def test_the_meter_refuses_a_route_the_repo_does_not_list(cfg: Config, store: Store) -> None:
    from vigild.budget import Budget
    from vigild.config import Secrets
    from vigild.gateway.service import Gateway, GatewayError

    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({}))
    client = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
    jev = JevOpenRouter(client, token="t", meter=gw.metered)
    with pytest.raises(GatewayError, match="jev-openrouter"):
        jev.ask(QUESTIONS["size"], "x", repo=PA, job_id=None)


def test_decider_checks_the_route_before_asking(cfg: Config, store: Store) -> None:
    cfg.repos.repos[PA].decider = ["haiku"]
    jev, haiku = Fixed("jev-cloudflare", 0.99), Fixed("haiku", 0.9)
    d = Decider(cfg, store, {"jev-cloudflare": jev, "haiku": haiku})
    assert d.decide(PA, QUESTIONS["size"], "x").answer.backend == "haiku" and jev.calls == []


# --- review round 2, N9 --------------------------------------------------------------------


def test_jev_cannot_be_built_without_a_meter() -> None:
    client = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
    with pytest.raises(TypeError):
        JevCloudflare(client, account_id="a", token="t")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        JevOpenRouter(client, token="t")  # type: ignore[call-arg]


def test_a_gateway_refusal_is_a_decider_failure(cfg: Config, store: Store) -> None:
    from vigild.gateway.service import GatewayError

    class Refused:
        name = "jev-cloudflare"

        def ask(self, q: Question, state: str, *, repo: str, job_id: str | None) -> Answer:
            raise GatewayError(403, "permission_error", "budget: the day is spent")

    haiku = Fixed("haiku", 0.95)
    d = Decider(cfg, store, {"jev-cloudflare": Refused(), "haiku": haiku}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert haiku.calls == ["size"] and d.answer is not None and d.answer.backend == "haiku"
    errors = [e.data for e in store.events() if e.type == "decision" and "error" in e.data]
    assert any("budget" in e["error"] for e in errors)


def test_the_production_wiring_meters_every_route(cfg: Config, store: Store) -> None:
    from vigild.budget import Budget
    from vigild.config import Secrets
    from vigild.decider.wiring import build_decider
    from vigild.gateway.service import Gateway

    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({}))
    secrets = Secrets(
        {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t", "OPENROUTER_API_KEY": "k"}
    )
    client = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
    d = build_decider(cfg, store, gw, secrets, client)
    assert set(d.backends) == {"haiku", "jev-cloudflare", "jev-openrouter"}
    for name in ("jev-cloudflare", "jev-openrouter"):
        assert d.backends[name].meter == gw.metered  # type: ignore[attr-defined]
    assert set(build_decider(cfg, store, gw, Secrets({}), client).backends) == {"haiku"}
