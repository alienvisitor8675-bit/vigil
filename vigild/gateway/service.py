"""The model gateway (P1, owner 2026-10-22).

Containers get a per-session gateway token, never a provider key. The
gateway:
- resolves the token to its session, repo and backend preset;
- enforces the private-repo backend rule on the wire, per call;
- refuses a model the repo's backends don't list, or one with no price;
- refuses a call whose worst case would take the session past its budget
  reservation, before the call is sent;
- injects the provider key and forwards;
- meters the call (tokens in, out, cache read, cache write; price-table cost;
  the provider's own cost if it reports one).

vigild's own model calls (the Haiku decider) go through `call()`, in process,
under the same checks plus a per-call reservation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx2

from vigild.budget import Budget, BudgetExceeded
from vigild.config import Config, Preset, Secrets
from vigild.db import Store, iso, utcnow
from vigild.metering.calls import CallUsage, SSEUsageTap, record_call, usage_from_anthropic
from vigild.metering.prices import PriceTable
from vigild.provenance import Provenance
from vigild.redact import redact
from vigild.repos import RepoPolicyError, check_backend, check_decider_route, live_private

FORWARD_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")
# Review round 1, item 5: only plain Messages requests pass. Server tools and
# MCP connectors would let Anthropic fetch from or post to hosts the session's
# proxy never allowed, and run up fees the gateway doesn't meter.
ALLOWED_BODY_KEYS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "metadata",
        "stop_sequences",
        "stream",
        "temperature",
        "top_p",
        "top_k",
        "tools",
        "tool_choice",
        "thinking",
        "output_config",
        "context_management",
    }
)
CLIENT_TOOL_TYPES = re.compile(r"^(custom|bash_\d{8}|text_editor_\d{8}|memory_\d{8})$")
# Betas Claude Code 2.1.306 sends (observed through the gateway, 2026-10-22),
# plus the ones vigild's own calls use. Anything else is refused by name.
ALLOWED_BETAS = frozenset(
    {
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "fine-grained-tool-streaming-2025-05-14",
        "context-management-2025-06-27",
        "prompt-caching-scope-2026-01-05",
        "effort-2025-11-24",
        # Also sent by CLI 2.1.306, seen at the gateway in the live guard test.
        # advanced-tool-use only matters with server tools, which _check_body refuses.
        "thinking-token-count-2026-05-13",
        "per-turn-control-2026-07-01",
        "mid-conversation-tool-changes-2026-07-01",
        "advanced-tool-use-2025-11-20",
        "thinking-display-updates-2026-08-18",
    }
)
# Review round 2, N4: content blocks are allowlisted at every level. A URL
# source would have Anthropic fetch a host the session's proxy never allowed;
# server-tool, MCP and search blocks carry content from tools vigild doesn't run.
# Thinking blocks are the model's own output, echoed back in assistant turns.
MESSAGE_BLOCKS = frozenset({"text", "image", "document", "tool_use", "tool_result"})
ASSISTANT_ONLY_BLOCKS = frozenset({"thinking", "redacted_thinking"})
# Owner decision 2026-10-22 (review round 3): thinking blocks are allowed, in
# assistant turns only, with exactly their own fields and nothing else.
THINKING_FIELDS = {
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
}
TOOL_RESULT_BLOCKS = frozenset({"text", "image", "document"})
SYSTEM_BLOCKS = frozenset({"text"})
SOURCED_BLOCKS = frozenset({"image", "document"})
# The CLI's own small-model side calls (titles, summaries) use this model; it is
# the one model a session may call besides its routed one, where the repo allows it.
SIDE_MODELS = frozenset({"claude-haiku-5-5"})
TOKEN_TTL_MARGIN_MIN = 15
METERED_PATHS = ("/v1/messages",)
FREE_PATHS = ("/v1/messages/count_tokens",)
CHARS_PER_TOKEN = 3  # deliberately low, so the input estimate errs high
DEFAULT_MAX_TOKENS = 4096
UPSTREAM_TIMEOUT = httpx2.Timeout(600.0, connect=10.0)


def _refuse_block(where: str, why: str) -> GatewayError:
    return GatewayError(403, "permission_error", f"{where}: {why}")


def _check_blocks(blocks: Any, allowed: frozenset[str], where: str) -> None:
    if isinstance(blocks, str):
        return
    if not isinstance(blocks, list):
        raise GatewayError(400, "invalid_request_error", f"{where} must be a string or a list")
    for i, b in enumerate(blocks):
        at = f"{where}[{i}]"
        kind = b.get("type") if isinstance(b, dict) else None
        if kind not in allowed:
            raise _refuse_block(at, f"content block {kind!r} is not allowed")
        if kind in THINKING_FIELDS and set(b) - THINKING_FIELDS[kind]:
            extra = sorted(set(b) - THINKING_FIELDS[kind])
            raise _refuse_block(at, f"{kind} block fields not allowed: {extra}")
        if kind in SOURCED_BLOCKS:
            src = b.get("source")
            if not isinstance(src, dict) or src.get("type") != "base64":
                got = src.get("type") if isinstance(src, dict) else None
                raise _refuse_block(at, f"{kind} source {got!r} is not allowed; base64 only")
        if kind == "tool_result":
            _check_blocks(b.get("content", ""), TOOL_RESULT_BLOCKS, f"{at}.content")


def check_content(body: dict[str, Any]) -> None:
    """Walk `system` and every message's content (review round 2, N4)."""
    if "system" in body:
        _check_blocks(body["system"], SYSTEM_BLOCKS, "system")
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise GatewayError(400, "invalid_request_error", "messages must be a list")
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise GatewayError(400, "invalid_request_error", f"messages[{i}] must be an object")
        allowed = MESSAGE_BLOCKS | (
            ASSISTANT_ONLY_BLOCKS if m.get("role") == "assistant" else set()
        )
        _check_blocks(m.get("content", ""), frozenset(allowed), f"messages[{i}].content")


class GatewayError(Exception):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message

    def body(self) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {"type": self.kind, "message": f"vigil gateway: {self.message}"},
        }


@dataclass(frozen=True)
class GatewaySession:
    session_id: str
    repo: str
    preset: str
    task_id: str | None
    job_id: str | None
    model: str = ""
    private: bool = True  # read live when the token was issued (fail closed)


@dataclass
class Hold:
    """One in-flight call's worst case, held against its session until it settles."""

    session_id: str
    amount: float
    preset: Preset
    body: dict[str, Any]
    released: bool = False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Gateway:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        budget: Budget,
        secrets_: Secrets,
        *,
        transport: httpx2.BaseTransport | None = None,
        async_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.budget = budget
        self.secrets = secrets_
        self.prices = PriceTable(cfg.prices)
        self.sync_client = httpx2.Client(transport=transport, timeout=UPSTREAM_TIMEOUT)
        self.async_client = httpx2.AsyncClient(transport=async_transport, timeout=UPSTREAM_TIMEOUT)
        self._inflight: dict[str, float] = {}
        self._inflight_lock = threading.Lock()
        # vigild's circuit breakers (plan 2a, item 4): every upstream answer
        # counts for or against the preset's backend breaker
        self.breakers: Any = None

    def _upstream(self, preset_name: str, ok: bool, detail: str = "") -> None:
        """N 5xx or timeouts in a row trip the preset's backend breaker; any
        other answer ends the run (and closes a half-open one)."""
        if self.breakers is None:
            return
        from vigild.breakers import Kind

        scope = f"backend:{preset_name}"
        if ok:
            self.breakers.success(Kind.BACKEND, scope)
        else:
            self.breakers.failure(
                Kind.BACKEND, scope, self.cfg.vigild.breakers.backend_errors,
                {"preset": preset_name, "error": detail[:300]},
            )  # fmt: skip

    # --- tokens -------------------------------------------------------------------

    def issue_token(self, session_id: str, repo: str, preset: str, *, ttl_minutes: int = 60) -> str:
        """A session's token: its repo, its preset's model, and a life of the
        session's own limit plus a margin (review round 1, item 7). The repo's
        visibility is read live here, at dispatch, and kept with the token for
        the session's per-call checks (owner, 2026-10-23)."""
        private = live_private(repo)
        check_backend(self.cfg, repo, preset, private=private)
        token = "vgw_" + secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(minutes=ttl_minutes)
        self.store.x(
            "INSERT INTO gateway_tokens (token_hash, session_id, repo, backend_preset, model,"
            " created_at, expires_at, private) VALUES (?,?,?,?,?,?,?,?)",
            (
                _hash(token),
                session_id,
                repo,
                preset,
                self.cfg.preset(preset).model or "",
                iso(),
                iso(expires),
                int(private),
            ),
        )
        return token

    def revoke_all(self) -> int:
        """At startup: no token from before a restart survives it."""
        rows = self.store.q("SELECT COUNT(*) n FROM gateway_tokens WHERE revoked_at IS NULL")
        self.store.x("UPDATE gateway_tokens SET revoked_at=? WHERE revoked_at IS NULL", (iso(),))
        return int(rows[0]["n"])

    def revoke(self, session_id: str) -> None:
        self.store.x(
            "UPDATE gateway_tokens SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (iso(), session_id),
        )

    def authorize(self, token: str | None) -> GatewaySession:
        if not token:
            raise GatewayError(401, "authentication_error", "missing session token")
        rows = self.store.q(
            "SELECT g.session_id, g.repo, g.backend_preset, g.revoked_at, g.model, g.expires_at,"
            " g.private, s.task_id, s.job_id"
            " FROM gateway_tokens g LEFT JOIN sessions s ON s.id = g.session_id"
            " WHERE g.token_hash=?",
            (_hash(token),),
        )
        if not rows or rows[0]["revoked_at"]:
            raise GatewayError(401, "authentication_error", "invalid or revoked session token")
        r = rows[0]
        if r["expires_at"] and datetime.fromisoformat(r["expires_at"]) <= utcnow():
            raise GatewayError(401, "authentication_error", "session token expired")
        return GatewaySession(
            r["session_id"], r["repo"], r["backend_preset"], r["task_id"], r["job_id"], r["model"],
            private=r["private"] is None or bool(r["private"]),
        )  # fmt: skip

    # --- checks ---------------------------------------------------------------------

    def _preset(self, repo: str, preset_name: str, *, private: bool) -> Preset:
        try:
            check_backend(self.cfg, repo, preset_name, private=private)
        except RepoPolicyError as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        preset = self.cfg.preset(preset_name)
        if preset.kind.value != "anthropic":
            raise GatewayError(
                403, "permission_error", f"{preset.kind.value} backends arrive in a later phase"
            )
        return preset

    def _allowed_models(self, repo: str, provider: str) -> set[str]:
        pol = self.cfg.repo(repo)
        return {
            p.model
            for name in pol.backends
            if (p := self.cfg.backends.presets[name]).provider == provider and p.model
        }

    def _check_model(
        self, repo: str, preset: Preset, model: str | None, bound: str | None = None
    ) -> str:
        if not model:
            raise GatewayError(400, "invalid_request_error", "model is required")
        if bound and model != bound and model not in SIDE_MODELS:
            raise GatewayError(
                403, "permission_error", f"this session may call {bound}, not {model}"
            )
        if model not in self._allowed_models(repo, preset.provider):
            raise GatewayError(
                403,
                "permission_error",
                f"model {model} is not one {repo} may use (configs/repos.yaml)",
            )
        if not self.prices.has(model):
            raise GatewayError(403, "permission_error", f"model {model} has no price; refused")
        return model

    def worst_case(self, model: str, body: dict[str, Any], raw_len: int) -> float:
        """The most a call could cost: every input token written to the 1-hour
        cache (the dearest input rate), plus max_tokens of output."""
        est_in = math.ceil(raw_len / CHARS_PER_TOKEN)
        max_out = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        return self.prices.cost(model, CallUsage(tokens_cache_write_1h=est_in, tokens_out=max_out))

    def _check_body(self, body: dict[str, Any], headers: dict[str, str]) -> None:
        extra = sorted(set(body) - ALLOWED_BODY_KEYS)
        if extra:
            raise GatewayError(403, "permission_error", f"request fields not allowed: {extra}")
        mt = body.get("max_tokens", DEFAULT_MAX_TOKENS)
        if isinstance(mt, bool) or not isinstance(mt, int) or mt < 1:
            raise GatewayError(400, "invalid_request_error", "max_tokens must be an integer >= 1")
        check_content(body)
        for tool in body.get("tools") or []:
            kind = tool.get("type") if isinstance(tool, dict) else None
            if kind is not None and not CLIENT_TOOL_TYPES.match(str(kind)):
                raise GatewayError(403, "permission_error", f"server tool {kind} is not allowed")
        betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        refused = [b for b in betas if b not in ALLOWED_BETAS]
        if refused:
            raise GatewayError(403, "permission_error", f"betas not allowed: {refused}")

    def _hold(self, session_id: str, amount: float) -> float:
        """Reserve `amount` of the session's headroom for one in-flight call."""
        with self._inflight_lock:
            held = self._inflight.get(session_id, 0.0)
            headroom = self.budget.session_headroom(session_id) - held
            if amount > headroom:
                raise GatewayError(
                    403,
                    "permission_error",
                    f"budget: this call could cost ${amount:.4f}; the session has "
                    f"${headroom:.4f} of its reservation left after calls in flight",
                )
            self._inflight[session_id] = held + amount
            return amount

    def release(self, hold: Hold) -> None:
        if hold.released:
            return
        hold.released = True
        with self._inflight_lock:
            left = self._inflight.get(hold.session_id, 0.0) - hold.amount
            if left > 1e-12:
                self._inflight[hold.session_id] = left
            else:
                self._inflight.pop(hold.session_id, None)

    def _key(self, preset: Preset) -> str:
        key = self.secrets.get(preset.key_ref)
        if not key:
            raise GatewayError(503, "api_error", f"{preset.key_ref} is not set in the env file")
        return key

    def _refuse(self, gs: GatewaySession, err: GatewayError) -> GatewayError:
        self.store.add_event(
            "gateway_refused",
            redact({"status": err.status, "reason": err.message}),
            Provenance.HUMAN,
            job_id=gs.job_id,
            session_id=gs.session_id,
            task_id=gs.task_id,
        )
        return err

    def preflight(
        self, gs: GatewaySession, path: str, raw: bytes, headers: dict[str, str] | None = None
    ) -> Hold:
        if path not in METERED_PATHS + FREE_PATHS:
            raise GatewayError(404, "not_found_error", f"{path} is not served")
        try:
            preset = self._preset(gs.repo, gs.preset, private=gs.private)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                raise GatewayError(400, "invalid_request_error", "body is not JSON") from e
            if not isinstance(body, dict):
                raise GatewayError(400, "invalid_request_error", "body must be an object")
            self._check_body(body, headers or {})
            model = self._check_model(gs.repo, preset, body.get("model"), gs.model or None)
            amount = 0.0
            if path in METERED_PATHS:
                amount = self._hold(gs.session_id, self.worst_case(model, body, len(raw)))
        except GatewayError as e:
            raise self._refuse(gs, e) from None
        return Hold(gs.session_id, amount, preset, body)

    # --- forwarding ------------------------------------------------------------------

    def _headers(self, incoming: dict[str, str], key: str) -> dict[str, str]:
        out = {h: incoming[h] for h in FORWARD_HEADERS if h in incoming}
        out.setdefault("anthropic-version", "2023-06-01")
        out.setdefault("content-type", "application/json")
        out["x-api-key"] = key
        return out

    def _record(
        self,
        *,
        gs: GatewaySession | None,
        preset_name: str,
        preset: Preset,
        model: str,
        usage: CallUsage,
        started: float,
        purpose: str,
        job_id: str | None = None,
        provider_cost: float | None = None,
    ) -> float:
        return record_call(
            self.store,
            self.prices,
            session_id=gs.session_id if gs else None,
            task_id=gs.task_id if gs else None,
            job_id=gs.job_id if gs else job_id,
            purpose=purpose,
            provider=preset.provider,
            backend_preset=preset_name,
            model=model,
            usage=usage,
            provider_cost_usd=provider_cost,
            status=200,
            latency_ms=int((time.monotonic() - started) * 1000),
            provenance=Provenance.LOCAL if preset.locality == "local" else Provenance.CLAUDE,
        )

    async def forward(
        self, gs: GatewaySession, path: str, headers: dict[str, str], raw: bytes
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes] | bytes]:
        hold = self.preflight(gs, path, raw, headers)
        preset, body = hold.preset, hold.body
        try:
            key = self._key(preset)
            url = preset.base_url.rstrip("/") + path
            req = self.async_client.build_request(
                "POST", url, headers=self._headers(headers, key), content=raw
            )
            started = time.monotonic()
            resp = await self.async_client.send(req, stream=True)
        except httpx2.TransportError as e:  # a timeout or a dropped connection
            self.release(hold)
            self._upstream(gs.preset, False, type(e).__name__)
            raise
        except BaseException:
            self.release(hold)
            raise
        self._upstream(gs.preset, resp.status_code < 500, f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        out_headers = {"content-type": ctype} if ctype else {}
        if path in FREE_PATHS:
            data = await resp.aread()
            await resp.aclose()
            self.release(hold)
            return resp.status_code, out_headers, data
        if "text/event-stream" in ctype and resp.status_code == 200:
            tap = SSEUsageTap()

            async def gen() -> AsyncIterator[bytes]:
                try:
                    async for (
                        chunk
                    ) in resp.aiter_bytes():  # decoded: no content-encoding is passed on
                        tap.feed(chunk)
                        yield chunk
                finally:
                    # book the call, then let go of its hold (review round 2, N7)
                    try:
                        await resp.aclose()
                        self._record(
                            gs=gs,
                            preset_name=gs.preset,
                            preset=preset,
                            model=tap.model or body["model"],
                            usage=tap.usage(),
                            started=started,
                            purpose="session",
                        )
                    finally:
                        self.release(hold)

            return resp.status_code, out_headers, gen()
        try:
            data = await resp.aread()
            await resp.aclose()
            if resp.status_code == 200:
                try:
                    payload = json.loads(data)
                    usage = payload.get("usage") or {}
                    self._record(
                        gs=gs,
                        preset_name=gs.preset,
                        preset=preset,
                        model=payload.get("model") or body["model"],
                        usage=usage_from_anthropic(usage),
                        started=started,
                        purpose="session",
                        provider_cost=usage.get("cost"),
                    )
                except json.JSONDecodeError, AttributeError:
                    pass
        finally:
            self.release(hold)  # only after the call is booked (review round 2, N7)
        return resp.status_code, out_headers, data

    # --- vigild's own calls -------------------------------------------------------------

    def metered(
        self,
        *,
        repo: str,
        route: str,
        provider: str,
        model: str,
        purpose: str,
        job_id: str | None,
        run: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """A non-Anthropic call vigild makes itself (Jev): the same route check,
        reservation and per-call row as everything through the gateway (item 11).
        The repo's visibility is read again right before the call: a repo that
        went private since dispatch never reaches OpenRouter (owner, 2026-10-23)."""
        try:
            check_decider_route(self.cfg, repo, route, private=live_private(repo))
        except RepoPolicyError as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        try:
            rid = self.budget.reserve(
                session_id=None,
                task_id=None,
                amount=self.cfg.budgets.decider_call_usd,
                purpose=purpose,
            )
        except BudgetExceeded as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        started = time.monotonic()
        cost = 0.0
        try:
            payload = run()
            result = payload.get("result", payload) if isinstance(payload, dict) else {}
            usage = (
                (result.get("usage") if isinstance(result, dict) else None)
                or payload.get("usage")
                or {}
            )
            cost = record_call(
                self.store,
                self.prices,
                session_id=None,
                task_id=None,
                job_id=job_id,
                purpose=purpose,
                provider=provider,
                backend_preset=route,
                model=model,
                usage=CallUsage(
                    tokens_in=int(usage.get("input_tokens") or 0),
                    tokens_out=int(usage.get("output_tokens") or 0),
                ),
                provider_cost_usd=usage.get("cost"),
                latency_ms=int((time.monotonic() - started) * 1000),
                provenance=Provenance.CLAUDE,
            )
            return payload
        finally:
            self.budget.settle(rid, cost)

    def call(
        self, *, repo: str, preset: str, body: dict[str, Any], purpose: str, job_id: str | None
    ) -> dict[str, Any]:
        p = self._preset(repo, preset, private=live_private(repo))
        body = {**body, "model": p.model}
        raw = json.dumps(body).encode()
        model = self._check_model(repo, p, p.model)
        worst = self.worst_case(model, body, len(raw))
        amount = max(worst, self.cfg.budgets.decider_call_usd)
        try:
            rid = self.budget.reserve(session_id=None, task_id=None, amount=amount, purpose=purpose)
        except BudgetExceeded as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        started = time.monotonic()
        try:
            try:
                resp = self.sync_client.post(
                    p.base_url.rstrip("/") + "/v1/messages",
                    headers=self._headers({}, self._key(p)),
                    content=raw,
                )
            except httpx2.TransportError as e:
                self._upstream(preset, False, type(e).__name__)
                raise
            self._upstream(preset, resp.status_code < 500, f"HTTP {resp.status_code}")
            payload: dict[str, Any] = resp.json()
            if resp.status_code != 200:
                raise GatewayError(
                    resp.status_code,
                    "api_error",
                    f"upstream {resp.status_code}: {payload.get('error')}",
                )
            cost = self._record(
                gs=None,
                preset_name=preset,
                preset=p,
                model=payload.get("model") or model,
                usage=usage_from_anthropic(payload.get("usage") or {}),
                started=started,
                purpose=purpose,
                job_id=job_id,
            )
            self.budget.settle(rid, cost)
            return payload
        except BaseException:
            self.budget.settle(rid, 0.0)
            raise
