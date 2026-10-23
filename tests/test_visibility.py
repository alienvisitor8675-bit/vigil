"""Live visibility (owner, 2026-10-23): GitHub's answer is the only source,
read at start, at every dispatch and before every Jev call; a repo that
can't be read is private. Mocked: this test never calls GitHub."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from tests.decider.test_decider import Fixed
from tests.jobs.test_coord_loop import REPO, World, _approve, _failure_item
from tests.jobs.test_coord_loop import world as world
from vigild.budget import Budget
from vigild.config import Config, Secrets
from vigild.db import Store
from vigild.decider.backends.jev import JevOpenRouter
from vigild.decider.policy import Decider
from vigild.decider.questions import QUESTIONS
from vigild.gateway.service import Gateway
from vigild.repos import set_visibility_source

PA = "thomasbardhi01/parkagent"
OR = json.loads(
    (
        __import__("pathlib").Path(__file__).parent / "decider" / "fixtures" / "jev_openrouter.json"
    ).read_text()
)


def test_public_goes_to_openrouter_and_private_to_cloudflare(cfg: Config, store: Store) -> None:
    pub, priv = Fixed("jev-openrouter", 0.95), Fixed("jev-cloudflare", 0.95)
    backends = {"jev-openrouter": pub, "jev-cloudflare": priv}
    vigil = "thomasbardhi01/vigil"
    assert Decider(cfg, store, backends, private=lambda r: False).decide(
        vigil, QUESTIONS["size"], "x").answer.backend == "jev-openrouter"  # fmt: skip
    assert Decider(cfg, store, backends, private=lambda r: True).decide(
        vigil, QUESTIONS["size"], "x").answer.backend == "jev-cloudflare"  # fmt: skip


def test_a_lookup_error_never_reaches_openrouter(cfg: Config, store: Store) -> None:
    pub = Fixed("jev-openrouter", 0.95)

    def broken(repo: str) -> bool:
        raise OSError("GitHub is down")

    d = Decider(cfg, store, {"jev-openrouter": pub}, private=broken)
    got = d.decide("thomasbardhi01/vigil", QUESTIONS["size"], "x")
    assert pub.calls == [] and got.action.value == "wait"


def test_the_call_is_checked_against_the_decisions_one_read(cfg: Config, store: Store) -> None:
    """One visibility read per decision (review A10): the gateway checks the
    route right before the Jev call against that read, without a second one."""
    calls: list[httpx2.Request] = []
    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({}))
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda r: calls.append(r) or httpx2.Response(200, json=OR))
    )
    reads: list[str] = []
    set_visibility_source(lambda r: reads.append(r) or True)  # private
    jev = JevOpenRouter(client, token="t", version_pin="typesafe/jev-1.13-20260917",
                        meter=gw.metered)  # fmt: skip
    got = Decider(cfg, store, {"jev-openrouter": jev}).decide(PA, QUESTIONS["size"], "x")
    assert calls == [] and got.action.value == "wait" and reads == [PA]
    # a call made with a private read never reaches OpenRouter
    with pytest.raises(Exception, match="private"):
        jev.ask(QUESTIONS["size"], "x", repo=PA, job_id=None, private=True)
    assert calls == []


def test_a_repo_that_went_private_is_refused_at_dispatch(world: World) -> None:
    """o/fixture lists Claude backends: private, it breaks the rule. The
    dispatch is refused and the owner paged; nothing falls back."""
    w = world
    job_id = None
    real = w.runner.dispatch

    def flip(j: str) -> None:
        set_visibility_source(lambda r: r == REPO)
        real(j)

    w.runner.dispatch = flip  # type: ignore[method-assign]
    job_id = _approve(w)
    assert w.store.get_job(job_id).state.value == "blocked"
    assert not [s for s in w.store.list_sessions(job_id) if s.role.value == "worker"]
    item = _failure_item(w, job_id, "repo_policy")
    assert "is private; hosted backend" in json.dumps(item["body"]["errors"])
    assert item["body"]["actions"] == ["abandon"]


def test_the_token_keeps_the_visibility_read_at_dispatch(cfg: Config, store: Store) -> None:
    gw = Gateway(cfg, store, Budget(store, cfg.budgets), Secrets({}))
    s = store.create_session(None, __import__("vigild.jobs.model", fromlist=["Role"]).Role.WORKER)
    set_visibility_source(lambda r: False)
    tok = gw.issue_token(s.id, PA, "claude-sonnet")
    assert gw.authorize(tok).private is False
    set_visibility_source(lambda r: True)
    with pytest.raises(Exception, match="private"):
        gw.issue_token(s.id, PA, "claude-sonnet")  # private now: the rule refuses it
    assert Any
