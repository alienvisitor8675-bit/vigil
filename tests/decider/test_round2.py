"""2a round 2: the decider's answer shapes, its fallback booking and cap, one
visibility lookup per decision, and the drift page's atomicity (owner rulings
4-7; reviews A2, A8, A9, A10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.decider.test_decider import PA, Fixed, passthrough
from vigild.breakers import Breakers, Kind
from vigild.budget import Budget
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.decider.backends.jev import JevCloudflare, JevOpenRouter
from vigild.decider.policy import Action, Decider
from vigild.decider.questions import QUESTIONS, DeciderError
from vigild.gateway.service import Gateway, GatewayError

FIX = Path(__file__).parent / "fixtures"
CF = json.loads((FIX / "jev_cloudflare.json").read_text())
OR = json.loads((FIX / "jev_openrouter.json").read_text())
PIN_CF, PIN_OR = "jev-1.13.0", "typesafe/jev-1.13-20260917"
VIGIL = "thomasbardhi01/vigil"


def _client(payload: Any, status: int = 200) -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(status, json=payload))
    )


def _or(payload: Any = OR) -> JevOpenRouter:
    return JevOpenRouter(_client(payload), token="t", version_pin=PIN_OR, meter=passthrough)


def _cf(payload: Any = CF, status: int = 200) -> JevCloudflare:
    return JevCloudflare(_client(payload, status), account_id="a", token="t",
                         version_pin=PIN_CF, meter=passthrough)  # fmt: skip


# --- owner 5: three question types, three answer shapes ------------------------------------


def test_choice_parses_to_the_option_and_its_probabilities() -> None:
    a = _or().ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)
    assert a.kind == "choice" and a.value == "S" and a.probability == pytest.approx(0.62)
    assert a.probabilities == {"XS": 0.37, "S": 0.62, "M": 0.01, "L": 0}


def test_score_parses_to_a_number_on_its_scale() -> None:
    """Jev's `score` is on its anchors' 0-4 index; the answer is on the 1-5 scale."""
    a = _or().ask(QUESTIONS["value"], "x", repo=PA, job_id=None, private=False)
    assert a.kind == "score" and isinstance(a.value, float)
    assert a.value == pytest.approx(3.77) and a.probability == pytest.approx(0.7)


def test_noul_parses_to_yes_no_with_the_probability_of_true() -> None:
    a = _or().ask(QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False)
    assert a.kind == "noul" and a.value is False and a.probability == pytest.approx(0.17)
    assert a.probabilities == pytest.approx({"true": 0.17, "false": 0.83})


def test_noul_takes_p_true_from_true_and_false_probabilities_when_given() -> None:
    out = json.loads(json.dumps(OR))
    out["answers"]["touches_money_path"] = {
        "type": "noul", "noul": 0.9, "probabilities": {"true": 0.9, "false": 0.1}}  # fmt: skip
    a = _or(out).ask(QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False)
    assert a.value is True and a.probability == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("p_true", "action"), [(0.9, Action.ACT), (0.7, Action.ESCALATED), (0.3, Action.WAIT)]
)
def test_noul_thresholds_apply_to_p_true(
    cfg: Config, store: Store, p_true: float, action: Action
) -> None:
    out = json.loads(json.dumps(OR))
    out["answers"]["touches_money_path"]["noul"] = p_true
    Decider(cfg, store, {"jev-openrouter": _or(out), "haiku": Fixed("haiku", 0.5)}).decide(
        PA, QUESTIONS["touches_money_path"], "x"
    )
    first = store.q("SELECT action, kind FROM decisions ORDER BY id")[0]
    assert (first["action"], first["kind"]) == (action.value, "noul")


def test_a_noul_with_a_non_number_raises() -> None:
    out = json.loads(json.dumps(OR))
    out["answers"]["touches_money_path"]["noul"] = "yes"
    with pytest.raises(DeciderError):
        _or(out).ask(QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False)


# --- owner 6: type and confidence on every decision, and the report ------------------------


def test_every_decision_logs_its_type_and_confidence(cfg: Config, store: Store) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from vigild.telemetry.spans import Telemetry

    exp = InMemorySpanExporter()
    tel = Telemetry()
    tel.provider.add_span_processor(SimpleSpanProcessor(exp))
    d = Decider(cfg, store, {"jev-openrouter": _or()}, telemetry=tel, private=lambda r: False)
    for name in ("size", "value", "touches_money_path"):
        d.decide(PA, QUESTIONS[name], "x")
    rows = store.q("SELECT kind, probability FROM decisions ORDER BY id")
    assert [r["kind"] for r in rows] == ["choice", "score", "noul"]
    spans = [s for s in exp.get_finished_spans() if s.name == "vigil.decider"]
    assert [s.attributes["vigil.decider.kind"] for s in spans] == ["choice", "score", "noul"]
    assert [s.attributes["vigil.decider.confidence"] for s in spans] == pytest.approx(
        [0.62, 0.7, 0.17]
    )


def test_the_report_buckets_confidence_and_escalations_per_type(store: Store) -> None:
    from vigild.kpis import decider_report

    rows = [("size", "choice", 0.62, "jev-openrouter", "escalated"),
            ("size", "choice", 0.9, "haiku", "act"),
            ("size", "choice", 0.91, "jev-openrouter", "act"),
            ("value", "score", 0.7, "jev-openrouter", "escalated"),
            ("touches_money_path", "noul", 0.17, "jev-openrouter", "wait"),
            ("touches_money_path", "noul", 0.0, "no-answer", "wait")]  # fmt: skip
    for q, kind, p, backend, action in rows:
        store.x(
            "INSERT INTO decisions (question, kind, answer, probability, backend, model_version,"
            " action, ts, provenance) VALUES (?,?,'a',?,?,'v',?,'2026-10-23T00:00:00','claude')",
            (q, kind, p, backend, action),
        )
    r = decider_report(store)
    assert r["choice"]["n"] == 3 and r["choice"]["escalated"] == 1
    assert r["choice"]["buckets"] == {"0.6-0.7": 1, "0.9-1.0": 2}
    assert r["score"]["escalated"] == 1 and r["score"]["buckets"] == {"0.7-0.8": 1}
    assert r["noul"]["n"] == 1 and r["noul"]["buckets"] == {"0.1-0.2": 1}  # no-answer skipped
    assert r["noul"]["no_answer"] == 1


def test_the_report_is_on_the_api(client: Any, tokens: Any) -> None:
    from tests.conftest import auth

    r = client.get("/api/decider/report", headers=auth(tokens.read))
    assert r.status_code == 200 and set(r.json()) == {"choice", "score", "noul"}


# --- A2 / coord 9: Cloudflare 2021 is treated like drift ------------------------------------


def _pager(pages: list[dict[str, Any]]) -> Any:
    class Item:
        id = 1

    def pager(job: str | None, title: str, body: dict[str, Any]) -> Item:
        pages.append({"title": title, **body})
        return Item()

    return pager


def test_2021_pages_once_and_every_haiku_decision_while_open_is_fallback(
    cfg: Config, store: Store
) -> None:
    err = json.loads((FIX / "cloudflare_2021.json").read_text())
    pages: list[dict[str, Any]] = []
    breakers = Breakers(store, cfg, _pager(pages))
    haiku = Fixed("haiku", 0.99)
    d = Decider(cfg, store, {"jev-cloudflare": _cf(err, 402), "haiku": haiku},
                breakers=breakers, private=lambda r: True)  # fmt: skip
    first = d.decide(VIGIL, QUESTIONS["size"], "x")
    again = d.decide(VIGIL, QUESTIONS["size"], "x")
    assert first.answer.backend == again.answer.backend == "haiku"
    assert haiku.purposes == ["decider_fallback", "decider_fallback"]  # never `decider`
    (page,) = pages
    assert page["title"].endswith("decisions now on Haiku fallback")


def test_2021_with_no_haiku_pages_waiting_for_the_owner(cfg: Config, store: Store) -> None:
    err = json.loads((FIX / "cloudflare_2021.json").read_text())
    pages: list[dict[str, Any]] = []
    cfg.repos.repos[PA].backends = []
    d = Decider(cfg, store, {"jev-cloudflare": _cf(err, 402), "haiku": Fixed("haiku", 0.99)},
                breakers=Breakers(store, cfg, _pager(pages)), private=lambda r: True)  # fmt: skip
    assert d.decide(PA, QUESTIONS["size"], "x").action is Action.WAIT
    (page,) = pages
    assert page["title"].endswith("decisions now waiting for the owner")


# --- owner 4: the fallback cap, $1 a day per repo ------------------------------------------


class Anthropic:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        body = {"answer": "S", "probability": 0.99}
        return httpx2.Response(200, json={
            "model": "claude-haiku-5-5", "content": [{"type": "text", "text": json.dumps(body)}],
            "usage": {"input_tokens": 1000, "output_tokens": 50}})  # fmt: skip


def _gateway(cfg: Config, store: Store, pages: list[dict[str, Any]]) -> tuple[Gateway, Anthropic]:
    up = Anthropic()
    gw = Gateway(cfg, store, Budget(store, cfg.budgets),
                 Secrets({"ANTHROPIC_API_KEY": "sk-test"}),
                 transport=httpx2.MockTransport(up))  # fmt: skip
    gw.breakers = Breakers(store, cfg, _pager(pages))
    return gw, up


def _spend_fallback(cfg: Config, store: Store, repo: str, usd: float) -> None:
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    record_call(store, PriceTable(cfg.prices), session_id=None, task_id=None, job_id=None,
                purpose="decider_fallback", provider="anthropic", backend_preset="claude-haiku",
                model="claude-haiku-5-5", usage=CallUsage(), repo=repo)  # fmt: skip
    store.x("UPDATE model_calls SET cost_usd=? WHERE repo=?", (usd, repo))


def test_the_cap_is_in_budgets_yaml(cfg: Config) -> None:
    assert cfg.budgets.decider_fallback_per_repo_usd == 1.00


def test_a_fallback_call_that_would_cross_the_cap_is_refused_before_it_is_sent(
    cfg: Config, store: Store
) -> None:
    pages: list[dict[str, Any]] = []
    gw, up = _gateway(cfg, store, pages)
    _spend_fallback(cfg, store, PA, 0.999)
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": "x"}]}
    with pytest.raises(GatewayError, match="fallback"):
        gw.call(repo=PA, preset="claude-haiku", body=body, purpose="decider_fallback",
                job_id=None, private=False)  # fmt: skip
    assert up.calls == 0
    assert gw.breakers.is_open(Kind.DECIDER_FALLBACK, f"repo:{PA}")
    (page,) = pages
    assert page["breaker_kind"] == "decider_fallback_cap"
    # dispatch is held for that repo only
    assert gw.breakers.blocking(repo=PA) is not None
    assert gw.breakers.blocking(repo=VIGIL) is None
    # other purposes still go through; the repo's next fallback is refused at once
    gw.call(repo=PA, preset="claude-haiku", body=body, purpose="decider", job_id=None,
            private=False)  # fmt: skip
    assert up.calls == 1
    # a reset releases it
    b = gw.breakers.blocking(repo=PA)
    assert b is not None
    gw.breakers.reset(b.id)
    assert gw.breakers.blocking(repo=PA) is None


def test_fallback_spend_under_the_cap_goes_through_and_is_booked_to_the_repo(
    cfg: Config, store: Store
) -> None:
    gw, _up = _gateway(cfg, store, [])
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": "x"}]}
    gw.call(repo=PA, preset="claude-haiku", body=body, purpose="decider_fallback", job_id=None,
            private=False)  # fmt: skip
    row = store.q("SELECT purpose, repo FROM model_calls")[0]
    assert (row["purpose"], row["repo"]) == ("decider_fallback", PA)


# --- A10: one visibility lookup per decision --------------------------------------------------


def test_one_visibility_lookup_per_decision_even_with_an_escalation(
    cfg: Config, store: Store
) -> None:
    from vigild.repos import set_visibility_source

    lookups: list[str] = []

    def source(repo: str) -> bool:
        lookups.append(repo)
        return False

    set_visibility_source(source)
    pages: list[dict[str, Any]] = []
    gw, up = _gateway(cfg, store, pages)
    low = json.loads(json.dumps(OR))
    low["answers"]["size"]["probabilities"]["S"] = 0.7  # escalates to Haiku
    jev = JevOpenRouter(_client(low), token="t", version_pin=PIN_OR, meter=gw.metered)
    from vigild.decider.backends.haiku import HaikuBackend

    d = Decider(cfg, store, {"jev-openrouter": jev, "haiku": HaikuBackend(gw)})
    got = d.decide(PA, QUESTIONS["size"], "x")
    assert got.answer.backend == "haiku" and up.calls == 1
    assert lookups == [PA]


def test_a_lookup_error_takes_the_private_route_and_never_raises(cfg: Config, store: Store) -> None:
    def broken(repo: str) -> bool:
        raise OSError("GitHub is down")

    pub, priv = Fixed("jev-openrouter", 0.95), Fixed("jev-cloudflare", 0.95)
    d = Decider(cfg, store, {"jev-openrouter": pub, "jev-cloudflare": priv}, private=broken)
    got = d.decide(VIGIL, QUESTIONS["size"], "x")
    assert pub.calls == [] and got.answer.backend == "jev-cloudflare"


# --- A8: every Jev route has a version pin ----------------------------------------------------


def test_a_jev_route_without_a_version_pin_is_refused_at_load() -> None:
    from pydantic import ValidationError

    from vigild.config import DeciderRoute

    with pytest.raises(ValidationError, match="version_pin"):
        DeciderRoute(backend="jev_openrouter", provider="openrouter", locality="hosted")
    DeciderRoute(backend="haiku", provider="anthropic", locality="hosted")  # not Jev: fine


def test_jev_backends_require_a_pin() -> None:
    with pytest.raises(TypeError):
        JevOpenRouter(_client(OR), token="t", meter=passthrough)  # type: ignore[call-arg]


# --- A9: the drift row and its page are one transaction ---------------------------------------


def test_a_crash_between_the_drift_row_and_the_page_still_pages(cfg: Config, store: Store) -> None:
    drifted = json.loads(json.dumps(OR))
    drifted["model"] = "jev-9"
    real = store.create_inbox_item
    calls = {"n": 0}

    def crash_once(*a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("vigild died here")
        return real(*a, **kw)

    store.create_inbox_item = crash_once  # type: ignore[method-assign]
    d = Decider(cfg, store, {"jev-openrouter": _or(drifted)})
    with pytest.raises(RuntimeError):
        d.decide(PA, QUESTIONS["size"], "x")
    assert store.q("SELECT COUNT(*) n FROM jev_drift")[0]["n"] == 0  # rolled back
    d.decide(PA, QUESTIONS["size"], "x")  # the next drifting call pages
    items = [i for i in store.list_inbox() if i.body.get("kind") == "jev_drift"]
    assert len(items) == 1


# --- owner 7: the pins are the strings the live responses report ----------------------------


def test_each_routes_version_pin_is_what_its_recorded_live_response_reports(cfg: Config) -> None:
    routes = cfg.backends.decider_routes
    assert routes["jev-openrouter"].version_pin == OR["model"] == PIN_OR
    assert routes["jev-cloudflare"].version_pin == CF["result"]["result"]["model"] == PIN_CF
    assert "state" in CF["result"]  # Cloudflare's run state sits at result.state


# --- 2a round 3 --------------------------------------------------------------------------------


def test_cloudflare_is_not_called_while_its_credit_breaker_is_open(
    cfg: Config, store: Store
) -> None:
    breakers = Breakers(store, cfg, _pager([]))
    breakers.trip(Kind.DECIDER_CREDIT, "route:jev-cloudflare", {"code": 2021})
    cf, haiku = Fixed("jev-cloudflare", 0.99), Fixed("haiku", 0.99)
    d = Decider(cfg, store, {"jev-cloudflare": cf, "haiku": haiku}, breakers=breakers,
                private=lambda r: True)  # fmt: skip
    got = d.decide(VIGIL, QUESTIONS["size"], "x")
    assert cf.calls == [] and got.answer.backend == "haiku"
    assert haiku.purposes == ["decider_fallback"]


def test_a_non_numeric_option_probability_is_a_decider_error(cfg: Config, store: Store) -> None:
    """2a round 3, nit 8: a plain ValueError skipped the fallback."""
    with pytest.raises(DeciderError):
        QUESTIONS["size"].parse("S", 0.9, backend="x", probabilities={"S": "high"})
    bad = json.loads(json.dumps(OR))
    bad["answers"]["size"]["probabilities"]["XS"] = "n/a"
    haiku = Fixed("haiku", 0.99)
    got = Decider(cfg, store, {"jev-openrouter": _or(bad), "haiku": haiku}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert got.answer.backend == "haiku" and haiku.purposes == ["decider_fallback"]


@pytest.mark.parametrize("shape", [["S", 0.6], "S=0.6"])
def test_probabilities_of_the_wrong_shape_are_a_decider_error(shape: Any) -> None:
    """2a round 6, nit 9: a list or a string raised AttributeError."""
    with pytest.raises(DeciderError):
        QUESTIONS["size"].parse("S", 0.6, backend="x", probabilities=shape)
    bad = json.loads(json.dumps(OR))
    bad["answers"]["size"]["probabilities"] = shape
    with pytest.raises(DeciderError):
        _or(bad).ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)
