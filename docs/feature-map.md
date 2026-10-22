# Feature map

One row per Vigil feature: the phase that builds it, how you reach it,
where its code lives, which guardrail binds it, what tests it, and whether
it is live. Read this before exploring the repo. When you change a
feature, update its row in the same PR, and update `docs/architecture.md`
if the change moves a flow shown there.

Nothing is built yet: every path below is where the code **will** live
(the layout in `docs/design.md` "Repo layout and build order"). A phase's PR turns
its rows' status to `shipped` and replaces any path it put somewhere
else. Phase 1 adds `scripts/check-feature-map.py` to CI (the same idea as
ParkAgent's `check-feature-map.mjs`): a path in a `shipped` row that
doesn't exist, or an API route in no row, fails it.

**Columns.**
- **Phase**: 1 Core · 2 Review + fleet + CLI/MCP · 3 Web UI + metering
  endpoint · 4 OpenCode executor · 5 Tower · 6 Eval.
- **Entry points**: REST route (`/api/...`), CLI command, MCP tool, web
  page, or GitHub trigger. All four reach the same handler; a CLI command
  or MCP tool is never its own code path.
- **Guardrails**: where a hard stop is enforced (budget reservation, WIP
  limit, spec gate, intake gate, money-path rule, private-repo backend
  rule, security layer).
- **Status**: `planned (phase N)`, `shipped`, or `blocked by <what>`.

## Intake and routing

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| Task contract | 1 | One JSON schema in, one result out, one event stream; the only thing crossing the vigild–executor boundary. | — | `vigild/contract/schema.py`, `schemas/task.schema.json`, `schemas/result.schema.json`, `schemas/event.schema.json` | an executor that returns anything else fails the contract suite | `tests/contract/` (run against every executor) | planned (phase 1) |
| Spec gate | 1 | Rejects a task without `test_cmd` and `done_when`; a spec-less issue becomes a spec-writing task. | `POST /api/tasks`; `vigil run`; MCP `vigil_run`; UI New session | `vigild/intake/spec_gate.py` | routing rule 1 | `tests/intake/test_spec_gate.py` | planned (phase 1) |
| Intake scoring | 1 | Every task carries `why`, `value` (1–5), `risk` (1–5); self-generated tasks need `value ≥ 3` and never touch money/auth/consent paths. | same as spec gate | `vigild/intake/scoring.py` | intake gate | `tests/intake/test_scoring.py` (both rules) | planned (phase 1) |
| Decider | 1 | Typed questions (choice, score, bool) with a probability; acts at ≥ 0.85, escalates to Haiku at ≥ 0.6, else waits for you. | internal | `vigild/decider/questions.py`, `vigild/decider/backends/{jev_cloudflare,haiku,lfm_tower}.py` | private repos only reach backends listed for them in `configs/repos.yaml` | `tests/decider/` (one schema, every backend; recorded fixtures) | planned (phase 1: Haiku live, Jev blocked by the Cloudflare token; LFM phase 5) |
| Router | 1 | Tier rules top to bottom, first match wins, the rule's name goes to `tier_reason`; a tier resolves to a backend preset. | internal; `--tier` override | `vigild/router/rules.py`, `configs/routing.yaml` | rule 2 (money paths → Claude Opus) is not overridable | `tests/router/test_rules.py` | planned (phase 1) |
| Backends | 1 | A session's model backend is `{kind, base_url, model, key_ref}`; presets for Claude API, tower, OpenRouter, custom. | `POST /api/tasks` (`backend`); UI New session | `vigild/backends.py`, `configs/backends.yaml` | an executor/backend pair not in the compatibility table is refused; a private repo is refused a backend not listed for it | `tests/test_backends.py` | planned (phase 1; OpenRouter phase 4, blocked by its key; tower phase 5) |
| GitHub label intake | 2 | An issue labeled `vigil:ready` becomes a task. | GitHub label (polled) | `vigild/intake/github.py` | WIP limit; intake gate | `tests/intake/test_github.py` | planned (phase 2) |

## Running sessions

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| Per-task checkout | 1 | A local mirror per repo; each task gets its own clone on branch `vigil/<task_id>-<slug>`, mounted at `/work`. | internal | `vigild/workspace.py` | `.github/`, `.claude/`, `.opencode/`, `.git/hooks`, `.git/config` mounted read-only over the clone | `tests/test_workspace.py` | planned (phase 1) |
| Container lifecycle | 1 | Starts, watches and removes one hardened container per session. | internal; `vigil kill` (phase 2) | `vigild/executors/runtime.py` | read-only root, non-root user, `--cap-drop ALL`, no-new-privileges, pids and memory caps, internal network only | `tests/security/` (one test per threat row) | planned (phase 1) |
| Egress allowlist | 1 | Each session sits on its own internal Docker network; a smokescreen sidecar is its only way out, with an allowlist generated for that session's role and backend. | — | `images/egress-proxy/`, `configs/egress.yaml` | a host not on the list can't be connected to (DNS is not the boundary) | `tests/security/test_egress.py` | planned (phase 1) |
| GitHub App tokens | 1 | vigild mints a one-hour installation token per task, scoped to the one repo and down-scoped permissions, revoked when the container exits. | internal | `vigild/github/app.py` | claude-mods tokens are always `contents: read`; no token ever carries `administration` | `tests/github/test_tokens.py` | planned (phase 1; blocked by the vigil-worker App) |
| Claude executor | 1 | Claude Agent SDK in a container, `bypassPermissions` inside the sandbox, guard plugin and deny list as managed settings, OTel on. | internal | `images/claude-executor/`, `vigild/executors/claude/` | guard@claude-mods at a pinned SHA; managed-settings deny list; SDK `max_budget_usd` / `max_turns` | `tests/contract/`, `tests/security/test_guard_in_container.py` | planned (phase 1) |
| OpenCode executor | 4 | `opencode serve` in a container, driven over HTTP and its SSE bus. | internal | `images/opencode-executor/`, `vigild/executors/opencode/` | `opencode.json` mirrors guard; same container limits | `tests/contract/`, `tests/security/` | planned (phase 4) |
| Scheduler | 2 | Dependency graph, parallel slots, retries and escalation (two tower failures → Claude). | internal | `vigild/scheduler/` | budget reservation before every start; WIP limit | `tests/scheduler/` | planned (phase 2) |
| Kill | 2 | Stops a session now: aborts the executor, removes the container, releases its reservation. | `POST /api/sessions/{id}/kill`; `vigil kill`; MCP `vigil_kill`; UI | `vigild/api/sessions.py` | — | `tests/api/test_kill.py` | planned (phase 2) |
| Self-maintenance | 2 | vigil may run tasks on its own repo, under the same rules and WIP limit. | same as any task | `configs/repos.yaml` | blocked until you decide the private-repo question (`docs/architecture.md` § Open decisions) | — | planned (after phase 2) |

## Review

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| Reviewer | 2 | Fresh-context Claude review of diff + issue + spec + checklist; returns `findings[]` and a verdict; posts a COMMENT review. | internal (CI green on a fleet PR) | `vigild/review/reviewer.py`, `images/claude-executor/` (reviewer prompt) | `edit: deny`; never approves | `tests/review/` | planned (phase 2) |
| Auditor and trust ladder | 2 | Opus audits reviews; match rates promote or demote a task shape's rung in `configs/review.yaml`. | internal | `vigild/review/ladder.py`, `configs/review.yaml` | money/auth/consent shapes can't be promoted | `tests/review/test_ladder.py` (demotion path) | planned (phase 2) |

## Budgets, metering, observability

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| Budgets | 1 | Daily, monthly (mandatory) and per-task Claude caps; each start reserves its task cap first, so the reservations can never sum past the day's cap. | `GET /api/budgets`; UI Budgets (phase 3) | `vigild/budget.py`, `configs/budgets.yaml` | hard stop: dispatch halts; reviews count | `tests/test_budget.py` (daily cap halts dispatch) | planned (phase 1) |
| WIP limit | 2 | At most 4 open fleet PRs per repo; the queue stops at the limit. | `GET /api/budgets`; UI Budgets | `vigild/scheduler/wip.py`, `configs/budgets.yaml` | hard stop | `tests/scheduler/test_wip.py` | planned (phase 2) |
| API metering | 1 | One row per model call (any provider): input, output, cache-read and cache-write tokens, cost. | read through `GET /api/metering` (phase 3) | `vigild/metering/calls.py`, `vigild/metering/prices.py`, `vigild/telemetry/otlp_receiver.py` | — | `tests/metering/` (per-call sum equals the SDK result's per-model total) | planned (phase 1) |
| Metering endpoint | 3 | Per-session and per-task metering as JSON, and Prometheus `/metrics`, for Hearth and the UI. | `GET /api/metering?task=…&session=…`; `GET /metrics` | `vigild/api/metering.py` | read token only | `tests/api/test_metering.py` | planned (phase 3) |
| GPU metering | 5 | GPU util, VRAM, CPU, RAM and watts each second from the tower, attributed to the session holding a llama-server slot; joules/token and GPU-seconds/token. | `GET /api/metering`; `GET /metrics`; UI session detail | `tower/exporter/`, `vigild/metering/gpu.py` | — | `tower/exporter/tests/`, `tests/metering/test_gpu_attribution.py` | planned (phase 5) |
| Tracing | 1 | One Langfuse trace per session; vigild's own spans in the same trace. | Langfuse UI; `vigil trace` (phase 2) | `vigild/telemetry/`, `compose.yaml`, `configs/otel-collector.yaml` | tool content logged only for the reviewer | `tests/telemetry/` | planned (phase 1; blocked by Langfuse keys) |
| Training-data firewall | after 6 | Every stored artifact carries `provenance`; the exporter takes only `local`. | — | `vigild/provenance.py` | a `claude`-tagged fixture is rejected | `tests/test_provenance.py` | planned (phase 1 adds the tag; exporter after phase 6) |

## Interfaces

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| REST API | 1 | The one API behind CLI, MCP and the web UI. | `/api/*` on the Mac's Tailscale address | `vigild/api/` | bearer token (admin or read) | `tests/api/` | planned (phase 1; `/api/tasks` and `/api/sessions` first) |
| Read views | 1–3 | The GET endpoints the CLI, MCP and UI read: tasks, their events and sessions, summary strip, repos, backends, ladder, decisions, calibration, budgets. | `GET /api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/events`, `/api/events/{id}`, `/api/sessions`, `/api/summary`, `/api/repos`, `/api/backends`, `/api/ladder`, `/api/decisions`, `/api/decider/calibration`, `/api/budgets`, `/api/health` | `vigild/api/` | read or admin token | `tests/api/` | planned (`/api/tasks*`, `/api/sessions`, `/api/budgets`, `/api/health` phase 1; the rest with the phase that adds their data) |
| Batch | 2 | Submit several tasks from one YAML file; each goes through the same gates as a single task. | `POST /api/batches`; `vigil batch`; UI `/new` (paste YAML) | `vigild/api/tasks.py` | same gates per task | `tests/api/test_batch.py` | planned (phase 2) |
| CLI | 2 | `vigil run · batch · ls --live · trace · kill`. | `vigil …` | `cli/vigil/` | — | `tests/cli/` | planned (phase 2) |
| MCP server | 2 | The same calls as MCP tools over stdio, so a Claude Code session can dispatch. | `vigil mcp`; tools `vigil_run`, `vigil_status`, `vigil_trace`, `vigil_kill` | `cli/vigil/mcp.py` | — | `tests/cli/test_mcp.py` | planned (phase 2) |
| Web UI | 3 | Surveillance-console UI, eight pages: board, job detail, sessions, decider, review ladder, new session, budget, talk. Character graphs, no build step, under 100 KB a page. Spec: `docs/ui.md`. | `/`, `/job/:id`, `/sessions`, `/decider`, `/review`, `/new`, `/budget`, `/talk` | `vigild/web/index.html`, `vigild/web/app.css`, `vigild/web/js/`, `vigild/web/assets/mark.txt` | same token as the API; calls only the endpoints `docs/ui.md` lists | `tests/web/` (glyph functions, page weight, endpoint list) | planned (phase 3) |
| Live stream | 3 | One server-sent-events stream per page, by topic (`job`, `summary`, `session`, `job:{id}`). | `GET /api/stream?topics=…` | `vigild/api/stream.py` | bearer token (read through `fetch`) | `tests/api/test_stream.py` | planned (phase 3) |
| Heartbeats | 3 | Outside sessions (vigil-coord, or ParkAgent's coord if you wire it) report what they are doing; shown on `/sessions`; expire after 5 min. | `POST /api/heartbeat`; `GET /api/heartbeats` | `vigild/api/heartbeat.py` | heartbeat-scoped token can do nothing else | `tests/api/test_heartbeat.py` | planned (phase 3) |
| Bridge (talk to coords) | 3 | One message bus per coordinator; the web UI's `/talk`, the MCP tools and `vigil-bridge` write to it, each message tagged `provenance: human` and its channel. Backend switch between turns starts a fresh session and is logged. Conversation only: no approve, merge or dispatch. | `/talk`; `GET /api/coords`, `GET /api/coords/{id}/thread`, `POST /api/coords/{id}/messages`, `POST /api/coords/{id}/backend`; MCP `vigil_talk`, `vigil_listen`; `vigil-bridge` | `vigild/bridge/`, `cli/vigil/bridge.py` | relay text logged verbatim with the relay model; local backend only on rung-3 shapes; decisions go to the inbox | `tests/bridge/` | planned (phase 3) |
| Decision labels | 3 | Your label on a decider answer; feeds agreement, calibration and the eval. | `POST /api/decisions/{id}/label`; `vigil label`; `/decider` | `vigild/decider/labels.py` | admin token | `tests/decider/test_labels.py` | planned (phase 3) |
| Coord status issue | — | The "vigil-coord status" issue in this repo; vigild's own per-repo status issues also live here, not in target repos. | GitHub issue #1 | — | — | — | shipped (#1) |

## Tower

| Feature | Phase | What it does | Entry points | Core code | Guardrails | Tests | Status |
|---|---|---|---|---|---|---|---|
| llama-server | 5 | Qwen3.6-35B-A3B main, gpt-oss-20b for XS; systemd unit bound to the tower's Tailscale address. | `http://<tower>:8080` | `tower/systemd/`, `tower/models.lock` | free VRAM checked before a load; 1 slot under 10 GB free | `tower/check.sh` | planned (phase 5) |
| Tower exporter | 5 | NVML + psutil sampled at 1 Hz; Prometheus and JSON. | `http://<tower>:9400` | `tower/exporter/` | Tailscale ACL: the Mac only | `tower/exporter/tests/` | planned (phase 5) |
| Eval | 6 | Replays closed issues across workers, reviewers and deciders; writes `evals/<yyyy-mm>/results.csv`. | `uv run evals/run.py` | `evals/` | its own one-time budget (see architecture) | `evals/tests/` | planned (phase 6) |
