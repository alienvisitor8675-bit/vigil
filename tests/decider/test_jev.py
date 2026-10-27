"""Jev's two routes against their recorded live shapes (owner, 2026-10-23;
recorded 2026-10-22): the request bodies, the nested parse, noul, a run that
didn't complete, and Cloudflare's 2021 (the AI Gateway credit is empty)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.decider.test_decider import PA, Fixed, passthrough
from vigild.config import Config
from vigild.db import Store
from vigild.decider.backends.jev import JevCloudflare, JevCreditExhausted, JevOpenRouter
from vigild.decider.policy import Action, Decider
from vigild.decider.questions import QUESTIONS, DeciderError

FIX = Path(__file__).parent / "fixtures"
CF = json.loads((FIX / "jev_cloudflare.json").read_text())
OR = json.loads((FIX / "jev_openrouter.json").read_text())


def _client(payload: Any, status: int = 200, seen: list[httpx2.Request] | None = None) -> Any:
    def handler(req: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(req)
        return httpx2.Response(status, json=payload)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _cf(payload: Any, status: int = 200, seen: list[httpx2.Request] | None = None) -> JevCloudflare:
    return JevCloudflare(
        _client(payload, status, seen),
        account_id="acct",
        token="t0k",
        version_pin="jev-1.13.0",
        meter=passthrough,
    )


def _or(payload: Any, seen: list[httpx2.Request] | None = None) -> JevOpenRouter:
    return JevOpenRouter(
        _client(payload, 200, seen),
        token="t0k",
        version_pin="typesafe/jev-1.13-20260917",
        meter=passthrough,
    )


def test_the_cloudflare_request_body() -> None:
    seen: list[httpx2.Request] = []
    _cf(CF, seen=seen).ask(
        QUESTIONS["size"], "Add three tests", repo=PA, job_id=None, private=False
    )
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/client/v4/accounts/acct/ai/run"
    assert req.headers["authorization"] == "Bearer t0k"
    body = json.loads(req.content)
    assert body == {
        "model": "typesafe/jev",
        "input": {
            "state": "Add three tests",
            "questions": {
                "size": {
                    "type": "choice",
                    "instructions": "How big is this task?",
                    "criteria": {"XS": "XS", "S": "S", "M": "M", "L": "L"},
                }
            },
        },
    }


def test_the_openrouter_request_body() -> None:
    seen: list[httpx2.Request] = []
    _or(OR, seen=seen).ask(
        QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False
    )
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/alpha/decisions"
    assert body["model"] == "typesafe/jev-1.13" and body["provider"]["zdr"] is True
    assert body["state"] == "x" and "input" not in body
    assert body["questions"]["touches_money_path"]["type"] == "noul"
    assert set(body["questions"]["touches_money_path"]["criteria"]) == {"true", "false"}


@pytest.mark.parametrize(
    ("jev", "version"), [(_cf, "jev-1.13.0"), (_or, "typesafe/jev-1.13-20260917")]
)
def test_the_nested_parse(jev: Any, version: str) -> None:
    payload = CF if jev is _cf else OR
    a = jev(payload).ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)
    assert a.value == "S" and a.model_version == version
    assert a.probability == (0.59 if jev is _cf else 0.62)
    v = jev(payload).ask(QUESTIONS["value"], "x", repo=PA, job_id=None, private=False)
    # a number on the 1-5 scale (owner ruling 5); the likeliest anchor's probability
    assert (v.value, v.probability) == (pytest.approx(3.76 if jev is _cf else 3.77), 0.7)
    n = jev(payload).ask(QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False)
    assert n.value is False and n.probability == (0.16 if jev is _cf else 0.17)


def test_a_run_that_didnt_complete_raises() -> None:
    bad = json.loads(json.dumps(CF))
    bad["result"]["state"] = "Failed"
    with pytest.raises(DeciderError, match="Failed"):
        _cf(bad).ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)


def test_no_version_is_recorded_as_unknown() -> None:
    """Every route is pinned (review A8), so no version is drift, as `unknown`."""
    from vigild.decider.backends.jev import JevVersionDrift

    bare = json.loads(json.dumps(OR))
    del bare["model"]
    with pytest.raises(JevVersionDrift) as e:
        _or(bare).ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)
    assert e.value.version == "unknown"


def _noul(value: float) -> dict[str, Any]:
    out = json.loads(json.dumps(OR))
    out["answers"]["touches_money_path"]["noul"] = value
    return out


@pytest.mark.parametrize(
    ("value", "action", "backend"),
    [
        (0.95, Action.ACT, "jev-openrouter"),
        (0.7, Action.ACT, "haiku"),
        (0.3, Action.WAIT, "jev-openrouter"),
    ],
)
def test_noul_is_a_float_the_thresholds_apply_to(
    cfg: Config, store: Store, value: float, action: Action, backend: str
) -> None:
    """0.95 acts; 0.7 escalates (to Haiku, which here acts); 0.3 waits."""
    haiku = Fixed("haiku", 0.9, 0.9)
    d = Decider(cfg, store, {"jev-openrouter": _or(_noul(value)), "haiku": haiku})
    got = d.decide(PA, QUESTIONS["touches_money_path"], "x")
    assert got.action is action and got.answer.backend == backend
    assert haiku.calls == (["touches_money_path"] if value == 0.7 else [])


@pytest.mark.parametrize("value", [1.4, -0.1, "yes"])
def test_a_noul_outside_0_to_1_raises(value: Any) -> None:
    with pytest.raises(DeciderError):
        _or(_noul(value)).ask(
            QUESTIONS["touches_money_path"], "x", repo=PA, job_id=None, private=False
        )


def test_2021_raises_credit_exhausted() -> None:
    """What the decider does with it (fall back, page once) is in test_round2.py."""
    err = json.loads((FIX / "cloudflare_2021.json").read_text())
    with pytest.raises(JevCreditExhausted):
        _cf(err, status=402).ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=False)


# --- version pin and drift (owner, 2026-10-23) ------------------------------------------------

PIN_OR = "typesafe/jev-1.13-20260917"


def _drifting(version: str | None) -> dict[str, Any]:
    out = json.loads(json.dumps(OR))
    if version is None:
        del out["model"]
    else:
        out["model"] = version
    return out


def _pinned(payload: dict[str, Any]) -> JevOpenRouter:
    return JevOpenRouter(_client(payload), token="t0k", version_pin=PIN_OR, meter=passthrough)


def _drift_items(store: Store) -> list[Any]:
    return [i for i in store.list_inbox() if i.body.get("kind") == "jev_drift"]


def test_the_pinned_version_acts_normally(cfg: Config, store: Store) -> None:
    haiku = Fixed("haiku", 0.99)
    sure = _drifting(PIN_OR)
    sure["answers"]["size"]["probabilities"]["S"] = 0.93
    d = Decider(cfg, store, {"jev-openrouter": _pinned(sure), "haiku": haiku})
    got = d.decide(PA, QUESTIONS["size"], "x")
    assert got.action is Action.ACT
    assert got.answer.backend == "jev-openrouter" and got.answer.model_version == PIN_OR
    assert haiku.calls == [] and _drift_items(store) == []


@pytest.mark.parametrize("version", ["typesafe/jev-1.13-20261101", "jev-1.13.1", None])
def test_another_version_or_none_falls_back_to_haiku_booked_as_fallback(
    cfg: Config, store: Store, version: str | None
) -> None:
    haiku = Fixed("haiku", 0.99)
    d = Decider(cfg, store, {"jev-openrouter": _pinned(_drifting(version)), "haiku": haiku})
    got = d.decide(PA, QUESTIONS["size"], "x")
    assert got.answer.backend == "haiku" and haiku.purposes == ["decider_fallback"]
    (item,) = _drift_items(store)
    shown = version or "unknown"
    assert item.title == (
        f"Jev version drift: jev-openrouter returned {shown}, pinned {PIN_OR}; "
        "decisions now on Haiku fallback"
    )
    assert "fallback_spend_today_usd" in item.body
    rows = store.q("SELECT backend, model_version, action FROM decisions ORDER BY id")
    assert (rows[0]["backend"], rows[0]["model_version"], rows[0]["action"]) == (
        "jev-openrouter", shown, "drift")  # fmt: skip


def test_an_escalation_still_books_decider(cfg: Config, store: Store) -> None:
    haiku = Fixed("haiku", 0.99)
    low = _drifting(PIN_OR)
    low["answers"]["size"]["probabilities"]["S"] = 0.7
    Decider(cfg, store, {"jev-openrouter": _pinned(low), "haiku": haiku}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert haiku.purposes == ["decider"]


def test_drift_pages_once_per_new_version_and_survives_a_restart(
    cfg: Config, tmp_path: Path
) -> None:
    db = tmp_path / "v.db"
    store = Store.open(db)
    for v in ["jev-9", "jev-9", "jev-9"]:
        Decider(cfg, store, {"jev-openrouter": _pinned(_drifting(v))}).decide(
            PA, QUESTIONS["size"], "x"
        )
    assert len(_drift_items(store)) == 1
    Decider(cfg, store, {"jev-openrouter": _pinned(_drifting("jev-10"))}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert len(_drift_items(store)) == 2
    reopened = Store.open(db)
    Decider(cfg, reopened, {"jev-openrouter": _pinned(_drifting("jev-9"))}).decide(
        PA, QUESTIONS["size"], "x"
    )
    assert len(_drift_items(reopened)) == 2


def test_with_nothing_else_in_the_chain_decisions_wait(cfg: Config, store: Store) -> None:
    """A private repo other than vigil can't use Haiku: drift means waiting."""
    cf = JevCloudflare(_client(CF), account_id="a", token="t", version_pin="jev-1.14.0",
                       meter=passthrough)  # fmt: skip
    d = Decider(cfg, store, {"jev-cloudflare": cf, "haiku": Fixed("haiku", 0.99)},
                private=lambda r: True)  # fmt: skip
    cfg.repos.repos[PA].backends = []
    got = d.decide(PA, QUESTIONS["size"], "x")
    assert got.action is Action.WAIT
    (item,) = _drift_items(store)
    assert item.title.endswith("decisions now waiting for the owner")


def test_the_version_lands_on_the_span_and_the_row_for_both_routes(
    cfg: Config, store: Store
) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from vigild.telemetry.spans import Telemetry

    exp = InMemorySpanExporter()
    tel = Telemetry()
    tel.provider.add_span_processor(SimpleSpanProcessor(exp))
    vigil = "thomasbardhi01/vigil"
    for private, jev in ((False, _or(OR)), (True, _cf(CF))):
        d = Decider(cfg, store, {jev.name: jev}, telemetry=tel, private=lambda r, p=private: p)
        d.decide(vigil, QUESTIONS["size"], "x")
    spans = [s for s in exp.get_finished_spans() if s.name == "vigil.decider"]
    got = [(s.attributes["vigil.decider.route"], s.attributes["vigil.decider.model_version"],
            s.attributes["vigil.repo.visibility"]) for s in spans]  # fmt: skip
    assert got == [("jev-openrouter", "typesafe/jev-1.13-20260917", "public"),
                   ("jev-cloudflare", "jev-1.13.0", "private")]  # fmt: skip
    rows = store.q("SELECT backend, model_version FROM decisions ORDER BY id")
    assert [(r["backend"], r["model_version"]) for r in rows] == [
        ("jev-openrouter", "typesafe/jev-1.13-20260917"), ("jev-cloudflare", "jev-1.13.0")]  # fmt: skip


def test_the_budget_shows_decider_and_fallback_spend_apart(cfg: Config, store: Store) -> None:
    from vigild.budget import Budget
    from vigild.metering.calls import CallUsage, record_call
    from vigild.metering.prices import PriceTable

    prices = PriceTable(cfg.prices)
    for purpose, n in (("decider", 1000), ("decider_fallback", 3000)):
        record_call(store, prices, session_id=None, task_id=None, job_id=None, purpose=purpose,
                    provider="anthropic", backend_preset="claude-haiku", model="claude-haiku-5-5",
                    usage=CallUsage(tokens_in=n))  # fmt: skip
    summary = Budget(store, cfg.budgets).summary()
    assert summary["decider_today"] == pytest.approx(1000 * 0.10 / 1e6)
    assert summary["decider_fallback_today"] == pytest.approx(3000 * 0.10 / 1e6)
