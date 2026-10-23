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
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
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

FALLBACK_PURPOSE = "decider_fallback"  # a Haiku decision Jev couldn't give (owner ruling 4)

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
UPSTREAM_READ_S = 600.0  # the longest an upstream call waits for its next bytes
UPSTREAM_TIMEOUT = httpx2.Timeout(UPSTREAM_READ_S, connect=10.0)
# a call in flight keeps its reservation live (2a round 9) until nothing of it
# has moved for the read timeout plus this: httpx has given up on it by then
LIVE_MARGIN_S = 120.0


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
    probe: str | None = None  # the backend breaker's half-open probe this call claimed


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
        self._fallback_pending: dict[str, float] = {}  # repo -> held for fallback calls
        self._inflight_lock = threading.Lock()
        self._drained = threading.Condition(self._inflight_lock)  # a session's holds all gone
        self._after_drain: dict[str, list[Callable[[], Any]]] = {}  # session -> settle
        self._seen: dict[str, float] = {}  # session -> when one of its calls last moved
        self._own: dict[int, float] = {}  # vigild's own call's reservation -> when it began
        self._now: Callable[[], float] = time.monotonic
        # vigild's circuit breakers (plan 2a, item 4): every upstream answer
        # counts for or against the preset's backend breaker
        self.breakers: Any = None

    def _backend_gate(self, preset_name: str) -> str | None:
        """An open backend breaker refuses the call before it is sent, unless
        it has cooled down and this call claims its one half-open probe
        (review B6, A11). Returns the claimed scope, to release if the call
        never reaches upstream."""
        if self.breakers is None:
            return None
        from vigild.breakers import Kind

        scope = f"backend:{preset_name}"
        if not self.breakers.is_open(Kind.BACKEND, scope):
            return None
        if self.breakers.claim_probe(Kind.BACKEND, scope):
            return scope
        raise GatewayError(529, "overloaded_error", f"{preset_name}: its backend breaker is open")

    def _release_probe(self, scope: str | None) -> None:
        if scope is not None and self.breakers is not None:
            from vigild.breakers import Kind

            self.breakers.release_probe(Kind.BACKEND, scope)

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
            if amount > headroom and self._over_cap_ok(session_id, amount - headroom):
                headroom = amount
            if amount > headroom:
                raise GatewayError(
                    403,
                    "permission_error",
                    f"budget: this call could cost ${amount:.4f}; the session has "
                    f"${headroom:.4f} of its reservation left after calls in flight",
                )
            self._inflight[session_id] = held + amount
            self._seen[session_id] = self._now()
            return amount

    def _touch(self, session_id: str) -> None:
        """A streamed call's bytes arrived: it is alive (2a round 9)."""
        self._seen[session_id] = self._now()

    def holds(self, session_id: str | None, reservation_id: int) -> bool:
        """Whether a call is still in flight against this reservation (2a
        round 9): one of the session's calls, a settle waiting for one, or
        vigild's own call. Bounded: a call nothing of which has moved for the
        upstream read timeout plus a margin is over, whatever its hold says.
        The runner's liveness rule asks this first."""
        stale = self._now() - (UPSTREAM_READ_S + LIVE_MARGIN_S)
        with self._inflight_lock:
            if reservation_id in self._own:
                return self._own[reservation_id] > stale
            if session_id and (session_id in self._inflight or session_id in self._after_drain):
                return self._seen.get(session_id, 0.0) > stale
        return False

    def _over_cap_ok(self, session_id: str, shortfall: float) -> bool:
        """A reviewer or auditor call past its session cap goes on (owner,
        2026-10-23): its reservation grows by the shortfall, against the day
        and the month, which stay hard stops. Any other session's cap is its
        cap. Raises GatewayError when the day or month can't take it."""
        from vigild.jobs.model import Role

        try:
            role = self.store.get_session(session_id).role
        except KeyError:
            return False
        if role not in (Role.REVIEWER, Role.AUDITOR):
            return False
        try:
            self.budget.reserve(
                session_id=session_id, task_id=None, amount=shortfall, purpose="review_over_cap"
            )
        except BudgetExceeded as e:
            raise GatewayError(403, "permission_error", f"budget: {e}") from e
        return True

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
                self._seen.pop(hold.session_id, None)
                self._drained.notify_all()
                later = self._after_drain.pop(hold.session_id, [])
        for fn in later if left <= 1e-12 else []:
            fn()  # a settle the runner left for when the late call was booked

    def after_drain(self, session_id: str, fn: Callable[[], Any]) -> None:
        """Run `fn` once none of the session's calls is in flight: now if
        none is, else when the last one is booked (2a round 8: a call that
        outlives the drain timeout still lands in the ledger, and the
        session's reservation stays open, and counted, until then)."""
        with self._inflight_lock:
            if session_id in self._inflight:
                self._after_drain.setdefault(session_id, []).append(fn)
                return
        fn()

    def drain(self, session_id: str, timeout: float = 60.0) -> bool:
        """Wait until none of the session's calls is in flight (each is booked
        before its hold goes), so settling sees every call's cost. A killed
        worker's last streamed call can finish after its container is gone
        (2a round 2, found by the done-when). False if the wait timed out."""
        with self._drained:
            return self._drained.wait_for(lambda: session_id not in self._inflight, timeout=timeout)

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
            try:
                probe = self._backend_gate(gs.preset)  # last: nothing refuses after a claim
            except GatewayError:
                if amount:
                    self.release(Hold(gs.session_id, amount, preset, body))
                raise
        except GatewayError as e:
            raise self._refuse(gs, e) from None
        return Hold(gs.session_id, amount, preset, body, probe=probe)

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
        repo: str | None = None,
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
            repo=gs.repo if gs else repo,
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
            self._release_probe(hold.probe)  # the call never reached upstream
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
                        self._touch(gs.session_id)
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
        private: bool | None = None,
        body_chars: int = 0,
    ) -> dict[str, Any]:
        """A non-Anthropic call vigild makes itself (Jev): the same route check,
        reservation and per-call row as everything through the gateway (item 11).
        The route is checked against the repo's visibility right before the
        call: the decision's own read when it passes one (one lookup per
        decision, review A10), else a fresh one. A repo read as private never
        reaches OpenRouter (owner, 2026-10-23)."""
        if private is None:
            private = live_private(repo)
        try:
            check_decider_route(self.cfg, repo, route, private=private)
        except RepoPolicyError as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        # the call's worst case, as the other gateway paths reserve it: its
        # input at the dearest input rate, twice over, never under the floor
        # (2a round 8: $0.01 flat let a bigger call settle past its row)
        worst = 2 * self.prices.cost(
            model, CallUsage(tokens_cache_write_1h=math.ceil(body_chars / CHARS_PER_TOKEN),
                             tokens_in=math.ceil(body_chars / CHARS_PER_TOKEN), tokens_out=1024)
        ) if self.prices.has(model) else 0.0  # fmt: skip
        amount = max(self.cfg.budgets.decider_call_usd, worst)
        with self._own_call(amount, purpose) as cost:
            started = time.monotonic()
            payload = run()
            result = payload.get("result", payload) if isinstance(payload, dict) else {}
            usage = (
                (result.get("usage") if isinstance(result, dict) else None)
                or payload.get("usage")
                or {}
            )
            cost[0] = record_call(
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
                repo=repo,
            )
            return payload

    @contextmanager
    def _own_call(self, amount: float, purpose: str) -> Iterator[list[float]]:
        """One of vigild's own calls (Jev through `metered`, Haiku through
        `call`): reserve its worst case, keep that reservation live while the
        call is in flight (2a round 9), then settle it at the call's cost,
        never past what was reserved. A cost past it is recorded
        (`settle_excess`) and trips the daily breaker (2a rounds 8-9), on
        both paths. The caller sets the yielded list's one item to the cost."""
        try:
            rid = self.budget.reserve(session_id=None, task_id=None, amount=amount, purpose=purpose)
        except BudgetExceeded as e:
            raise GatewayError(403, "permission_error", str(e)) from e
        with self._inflight_lock:
            self._own[rid] = self._now()
        cost = [0.0]
        try:
            yield cost
        finally:
            try:
                self.budget.settle(rid, cost[0])  # first: the sweep never sees it unheld
            finally:
                with self._inflight_lock:
                    self._own.pop(rid, None)
            if cost[0] > amount + 1e-9 and self.breakers is not None:
                from vigild.breakers import GLOBAL, Kind

                self.breakers.trip(
                    Kind.DAILY, GLOBAL,
                    {"why": "a call vigild made cost more than its reservation",
                     "reserved_usd": round(amount, 6), "cost_usd": round(cost[0], 6)},
                )  # fmt: skip

    def _fallback_today(self, repo: str) -> float:
        day = utcnow().strftime("%Y-%m-%d")
        rows = self.store.q(
            "SELECT COALESCE(SUM(cost_usd), 0) n FROM model_calls"
            " WHERE purpose=? AND repo=? AND ts LIKE ?",
            (FALLBACK_PURPOSE, repo, f"{day}%"),
        )
        return float(rows[0]["n"])

    def _fallback_hold(self, repo: str, amount: float) -> None:
        """Owner ruling 4: a repo's Haiku fallback spend has its own daily cap.
        A call that would cross it is refused before it is sent; the repo's
        fallback breaker trips, pages, and holds the repo's dispatch."""
        from vigild.breakers import Kind

        scope = f"repo:{repo}"
        cap = self.cfg.budgets.decider_fallback_per_repo_usd
        with self._inflight_lock:
            open_ = self.breakers is not None and self.breakers.is_open(
                Kind.DECIDER_FALLBACK, scope
            )
            spent = self._fallback_today(repo) + self._fallback_pending.get(repo, 0.0)
            if open_ or spent + amount > cap:
                if self.breakers is not None:
                    self.breakers.trip(
                        Kind.DECIDER_FALLBACK, scope,
                        {"spent_today_usd": round(spent, 6), "cap_usd": cap,
                         "next": "Jev has drifted or is failing: check the Jev pages, then reset"},
                    )  # fmt: skip
                raise GatewayError(
                    403, "permission_error",
                    f"decider fallback cap: {repo} has ${spent:.4f} of ${cap:.2f} today",
                )  # fmt: skip
            self._fallback_pending[repo] = self._fallback_pending.get(repo, 0.0) + amount

    def _fallback_done(self, repo: str, amount: float) -> None:
        with self._inflight_lock:
            self._fallback_pending[repo] = max(0.0, self._fallback_pending.get(repo, 0.0) - amount)

    def call(
        self,
        *,
        repo: str,
        preset: str,
        body: dict[str, Any],
        purpose: str,
        job_id: str | None,
        private: bool | None = None,
    ) -> dict[str, Any]:
        """vigild's own Anthropic call (the Haiku decider). `private` is the
        decision's visibility read (review A10); without it, read now."""
        if private is None:
            private = live_private(repo)
        p = self._preset(repo, preset, private=private)
        body = {**body, "model": p.model}
        raw = json.dumps(body).encode()
        model = self._check_model(repo, p, p.model)
        worst = self.worst_case(model, body, len(raw))
        amount = max(worst, self.cfg.budgets.decider_call_usd)
        fallback = purpose == FALLBACK_PURPOSE
        if fallback:
            self._fallback_hold(repo, amount)
        try:
            probe = self._backend_gate(preset)
            try:
                return self._call(repo, preset, p, model, raw, amount, purpose, job_id)
            except GatewayError:
                self._release_probe(probe)  # refused before upstream (the budget)
                raise
        finally:
            if fallback:
                self._fallback_done(repo, amount)

    def _call(
        self,
        repo: str,
        preset: str,
        p: Preset,
        model: str,
        raw: bytes,
        amount: float,
        purpose: str,
        job_id: str | None,
    ) -> dict[str, Any]:
        with self._own_call(amount, purpose) as cost:
            started = time.monotonic()
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
            cost[0] = self._record(
                gs=None,
                preset_name=preset,
                preset=p,
                model=payload.get("model") or model,
                usage=usage_from_anthropic(payload.get("usage") or {}),
                started=started,
                purpose=purpose,
                job_id=job_id,
                repo=repo,
            )
            return payload
