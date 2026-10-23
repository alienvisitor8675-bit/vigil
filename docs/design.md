# Vigil: design doc

Oct 22, 2026 · @Thomas Bardhi

> **Revised 2026-10-22 (PR 1, vigil-coord).** This revision applies your kickoff's changes:
> - Everything but model serving runs on the Mac mini.
> - Model backends are config objects.
> - A web UI and a metering endpoint are new phase 3.
> - Metering covers API tokens and tower power.
> - Vigil may work on its own repo after phase 2.
> - Six phases (now seven; see below).
> - The answers to the four open questions.
>
> - The design addendum (2026-10-22): "The factory floor" and "Job execution protocol", merged after the trust ladder. Where the addendum conflicted with corrections already here (per-task clones, OpenCode v2 endpoints), the corrections were kept, as the addendum directs.
>
> - The bridge ("Talking to coordinators from anywhere"): one message bus per coordinator, written to from the web UI's `/talk`, the vigil MCP server, and `vigil-bridge` for local chats.
>
> - The documentation standard ("context baked into every job"): an eight-layer doc tree, packet assembly from it, write-back at every stage, and freshness checks; placed after the job execution protocol.
>
> - **Owner decisions 2026-10-22** (phase 1 PR), marked where they land:
>   - Q1–Q3 and P1, P3–P6 answered; P2 yes; P7 no.
>   - Arize Phoenix replaces Langfuse.
>   - One GitHub App per role is deferred (`docs/decisions/ADR-0001-one-github-app-per-role.md`).
>   - Seven phases.
>   - The Cognition corrections, the documentation standard's phasing, the verification ladder, and the bridge's phasing.
>
> - **The learning loop (owner spec, 2026-10-23), design only:**
>   - a "Learning loop" section after the verification ladder;
>   - its stage changes, marked **Learning (planned)** in the job execution protocol;
>   - a learning epic after phase 5.
>
>   Where it contradicts the current design, a numbered **Open** note says so, for the owner to decide.
>
> It also corrects the facts the research disproved. How each part is built, the choices picked at build time, the full phase plan and the open decisions are in `docs/architecture.md`; versions and citations are in `docs/sources.md`.

## Summary

Vigil is one orchestrator that runs many isolated coding sessions in parallel across your repos, routes each task to a model backend (Claude, a local model on your Linux tower, OpenRouter, or a custom endpoint), and records every session as one trace you can open. Claude does first drafts, anything hard or on a money/auth path, and every review. OpenCode workers on local models served by the tower do routine work for free.

The operating rule: **a task is a contract, not a chat.** Every session gets a written spec with a test that must go from red to green, runs in its own container and worktree, cannot merge or deploy, and ends in a PR that a fresh-context Claude reviews before a human merges. Spawn from the CLI (`vigil run`), from a Claude Code session (an MCP tool), from the web UI, or from a GitHub label; all four hit the same API.

Stack, pinned at build time (`docs/sources.md`):
- **Orchestrator:** Python 3.14 (FastAPI + SQLite + a dependency-graph scheduler), running natively on the Mac mini.
- **Workers:** linux/arm64 worker containers under Docker Desktop. Each runs the Claude Agent SDK or an OpenCode v2 server.
- **Models:** llama.cpp `llama-server` on the tower, the only thing the tower runs besides a small metrics exporter.
- **Tracing:** OpenTelemetry, through a collector, into self-hosted Arize Phoenix.
- **Networking:** Tailscale.

## What an expert would insist on

These eight choices are where fleets fail or pay off. Each is a design constraint below, not advice.

1. **Local models fail on vague tasks, not on hard ones.** A 35B MoE can write a test from a spec; it cannot discover what the spec should be. So Claude writes specs, and the cheap tier only ever receives a task with a named test, named files and a definition of done. Routing is by *task shape*, not by how smart the model is.
2. **Isolation is physical, not configured.** OpenCode's permission JSON is friction, not a boundary; a model can `cat` what it was told not to `read`. The boundary is the container: read-only root, one worktree mounted, an egress allowlist, no secrets in the environment. Permissions only reduce wasted turns.
3. **The agent cannot grade itself.** Every PR gets a fresh-context Claude review that sees only the diff and the issue. Tests run in CI, not in the worker's shell, so a worker cannot weaken a test and call it green.
4. **One task contract for every executor.** Claude and OpenCode workers accept and return the same JSON. Swapping models or harnesses later costs nothing above a new executor plugin.
5. **Routing decisions are logged, and the eval tunes them.** Every `tier` choice carries a reason. Ten replayed issues per quarter decide which task shapes the tower keeps and which go back to Claude.
6. **Budgets are hard stops.** Daily Claude dollar cap and per-task token caps are enforced by the orchestrator, not by hope. Escalation from the tower to Claude happens after two failures, never silently.
7. **Traces are the product.** If you can't open a session and see every tool call, token and cost, you can't trust it unattended. Both tiers feed one trace store with one schema.
8. **Claude outputs never enter a training set.** A provenance tag on every artifact and a test that asserts the training pipeline rejects them.

## Architecture

Everything runs on the **Mac mini** (Apple Silicon, Docker Desktop, images built for linux/arm64) except model inference. The **Linux tower** (RTX 5070 Ti 16 GB, 32 GB DDR5) serves models only: `llama-server` plus a small metrics exporter, nothing else. The two reach each other over Tailscale.

| Component | Runs where | Job |
| --- | --- | --- |
| `vigild` (orchestrator) | Mac mini, native under launchd | Task queue, dependency graph, routing, backends, budgets, metering, container lifecycle, status issue, REST API, web UI |
| `vigil` CLI + MCP server | Mac mini (or any tailnet machine) | `vigil run`, `vigil ls`, `vigil trace`, `vigil kill`; the same calls exposed as MCP tools so a Claude Code session can dispatch |
| Web UI | served by `vigild`, Tailscale only | Nine pages (board, job detail, sessions, decider, review, new session, budget, talk, watch) per `docs/ui.md`; same API as the CLI and MCP |
| Claude executor | Mac mini, one container per task | Claude Agent SDK, headless, a per-session gateway token (no provider key, no GitHub token), guard plugin, OTel on |
| OpenCode executor | Mac mini, one container per task | `opencode serve` (v2) + the task's clone, pointed at the session's backend |
| `llama-server` | tower host (GPU) | Qwen3.6-35B-A3B main, gpt-oss-20b for small jobs, LFM2.5 decider; 2 slots |
| Tower exporter | tower host | GPU, VRAM, CPU, RAM and watts sampled each second |
| Reviewer | Mac mini, container per PR | Claude, fresh context, diff + issue + checklist only |
| Phoenix + OTel Collector | Mac mini, Docker Compose | One trace per session |

**Model backends.** A session's backend is a config object, not a tier name:
`{kind: anthropic | openai_compatible | openrouter, base_url, model, key_ref}`.
- **`key_ref`** names a variable in `~/.config/vigil/env`, never a value.
- **Built-in presets:**
  - Claude API (Sonnet, Opus, Haiku)
  - tower llama-server
  - OpenRouter (any model id)
  - a custom endpoint
  - Hearth's local models, as a preset pointing at the tower or whatever endpoint Hearth exposes
- **Routing tiers** map to presets in `configs/routing.yaml`. vigild, the router and the scheduler see only the preset name. Executor adapters read the object.

```mermaid
flowchart LR
  subgraph Sources
    GH[GitHub issue vigil:ready]
    YAML[batch.yaml]
    CLI[vigil CLI]
    CC[Claude Code session via MCP]
    WEB[Web UI]
  end
  GH --> D
  YAML --> D
  CLI --> D
  CC --> D
  WEB --> D
  subgraph vigild
    D[Dispatcher: spec gate] --> R[Router: tier rules]
    R --> Q[Queue + dep graph + budgets]
  end
  Q -->|tier=claude| CE[Claude executor container]
  Q -->|tier=local| OE[OpenCode executor container]
  OE --> LS[(llama-server on GPU)]
  CE --> API[(Anthropic API)]
  CE --> PR[PR on branch]
  OE --> PR
  PR --> CI[GitHub Actions]
  CI --> RV[Claude reviewer container]
  RV -->|findings| Q
  RV -->|ready| H[Human merges]
  CE -.OTel.-> OC[OTel Collector]
  OE -.SSE /event.-> OC
  RV -.OTel.-> OC
  OC --> LF[Phoenix]
  vigild --> SI[GitHub status issue]
  subgraph Learning["learning epic (planned, after phase 5)"]
    HT[(hidden tests · vigild only)]
    VR[reward computation · reward.json]
    PM[Post-merge: 30-day retroactive reward]
    MID[Middle loop: incident → playbook, eval case]
    RP[Weekly replay: Opus + best local]
  end
  D -.spec time.-> HT
  VF[vigild verifier · built, B33] -.raw reports.-> VR
  HT -.-> VF
  PR -.-> VF
  VR -.reward.json.-> RV
  H -.-> PM
  PM -.incident.-> MID
  MID -.playbooks into packets.-> Q
  RP -.-> VF
  VR -.episode, attempt, reward spans.-> OC
```

The loop: a task enters, the spec gate rejects it unless it has a test and a done condition, the router assigns a tier with a reason, the queue starts a container when a slot and budget allow, the worker pushes a branch and opens a draft PR, CI runs, the reviewer posts findings or marks the PR ready, and findings re-enter the queue as a follow-up task (same tier, or escalated). The dotted *learning epic* nodes are planned, not built ("Learning loop", below).

## Task contract

Every executor takes this in and hands this back. Nothing else crosses the boundary, so a new model or harness is one adapter.

```json
{
  "task_id": "pa-0142",
  "repo": "thomasbardhi01/parkagent",
  "base": "main",
  "branch": "vigil/pa-0142-rate-limit-tests",
  "tier": "local",
  "tier_reason": "label:tests + paths:tests/** + size:S",
  "backend": { "kind": "openai_compatible", "base_url": "http://tower:8080/v1", "model": "qwen3.6-35b-a3b", "key_ref": "LLAMA_API_KEY" },
  "why": "rate limiting has no tests; #139 changed it",
  "value": 4,
  "risk": 2,
  "spec": {
    "goal": "Add tests for rate limiting on /api/park",
    "test_cmd": "pytest tests/api/test_rate_limit.py -q",
    "must_fail_first": true,
    "files_in_scope": ["tests/api/test_rate_limit.py", "docs/feature-map.md"],
    "done_when": "test_cmd exits 0; docs/feature-map.md lists the limit",
    "out_of_scope": ["src/billing/**", "src/auth/**"]
  },
  "limits": { "max_turns": 60, "max_minutes": 40, "max_cost_usd": 4.00 },
  "depends_on": ["pa-0139"],
  "attempt": 1
}
```

Result:

```json
{
  "task_id": "pa-0142",
  "status": "pr_open | failed | escalate | timeout",
  "branch": "vigil/pa-0142-rate-limit-tests",
  "pr_url": "https://github.com/.../pull/312",
  "test_before": "fail", "test_after": "pass",
  "files_changed": ["tests/api/test_rate_limit.py", "docs/feature-map.md"],
  "summary": "3 tests; 429 after 10 req/min; docs updated",
  "usage": { "model": "qwen3.6-35b-a3b-q4", "tokens_in": 48210, "tokens_out": 6120, "tokens_cache_read": 31000, "tokens_cache_write": 0, "cost_usd": 0, "gpu_seconds": 1130, "energy_j": 312000 },
  "trace_id": "lf-9c1d...",
  "provenance": "local"
}
```

Alongside the result, every executor streams normalized events (`model_request`, `tool_call`, `tool_result`, `decision`, `state`) in one schema. The web UI's session timeline and the per-call metering read them.

The spec gate in `vigild` rejects a task without `test_cmd` and `done_when`. A GitHub issue without them is routed to Claude with the job "write the spec", which produces the contract above and re-queues the task; that is the cheapest Claude call in the system and the one that makes the tower useful.

## Intake: the fleet does not ship for the sake of shipping

A fleet that can produce PRs all night will fill your repos with PRs nobody wanted. The guard is at intake, not at review: no task runs until something has said why it is worth doing, and the fleet has a hard limit on open work.

1. **Every task carries a `why` and a `value` score (1–5) and a `risk` score (1–5).** Issues from you get these from the label set (`value:4`, `risk:2`). Issues the fleet generates (from an audit) must be scored by the decider model and are capped at `value ≥ 3` before they can enter the queue; the rest are filed as `vigil:proposed` for you to look at weekly.
2. **WIP limit per repo: 4 open fleet PRs.** When the limit is hit the queue stops, and the dashboard shows the stack waiting on you. Merging, not spawning, is the bottleneck by design.
3. **Audits produce findings, not tasks.** The nightly audit (lint, dead code, dependency age, docs drift, flaky tests, bug reports from the prod suite) writes one issue per finding, scored by the decider, and never dispatches work itself. Bugs with `risk ≥ 4` or a failing prod check page you instead of waiting for the weekly look.
4. **A weekly `vigil report`** lists what shipped, what was proposed and declined, cost, and escalation rate. If the escalation rate or declined-proposal rate climbs, the fleet is generating noise and the audit thresholds get tightened.
5. **Nothing self-generated touches a money, auth or consent path** without you creating the issue by hand.

The loop is: ship features you asked for → fix bugs the prod suite and audits find → audit → propose → you decide what enters next. The fleet repeats; you steer.

## Routing rules and escalation

Rules run top to bottom; the first match wins and its name is written to `tier_reason`. Size is estimated from the spec's `files_in_scope` and the issue body length.

| # | Condition | Tier | Model |
| --- | --- | --- | --- |
| 1 | Task has no `test_cmd` or `done_when` | claude | Sonnet (spec-writing only, no code) |
| 2 | Paths touch `src/billing/**`, `src/auth/**`, `**/payments/**`, `**/consent/**`, Stripe/Fly config | claude | Opus |
| 3 | Label `architecture`, `migration`, or touches `docs/architecture.md` structure | claude | Opus |
| 4 | New feature with no existing tests in scope | claude | Sonnet (first pass) |
| 5 | `attempt` ≥ 3, or two review rounds with findings | claude | Sonnet (escalated) |
| 6 | Label `tests`, `docs`, `deps`, `lint`, `copy`, `ui-small`; size S or M; ≥1 existing test in scope | local | Qwen3.6-35B-A3B |
| 7 | Size XS (one file, < 40 lines expected), docs or lint only | local-fast | gpt-oss-20b |
| 8 | Review follow-up on non-money code, size S | local | Qwen3.6-35B-A3B |
| 9 | Anything else | claude | Sonnet |

Reviews follow the trust ladder in the next section: Claude on everything at first, local reviewers graduating per task shape as their audits pass. Money-path PRs (rule 2) are always reviewed by Claude Opus and never graduate.

Escalation ladder for a `local` task: attempt 1 and 2 on the tower (second attempt gets the reviewer's findings appended to the spec); attempt 3 goes to Claude Sonnet with the prior diffs attached as context. A task that times out twice on the tower is re-sized to L and skips to Claude. All of this is written to the task record and to the status issue.

Two overrides you can set per task: `vigil run --tier claude` forces the tier, and a `vigil:local-ok` label on a money-path issue is refused (rule 2 is not overridable).

## Decider: typed decisions, not prose

The fleet makes hundreds of small closed decisions a day that are not coding: which tier, how big, how risky, is this proposal worth it, does this review need escalation, is this finding a duplicate. Those go to a **decider** with one interface: state in (issue text, spec, diff summary, or review JSON), typed questions out (a choice from a fixed set, a score, or a yes/no), each with a probability. `vigild` acts when confidence clears a threshold and escalates to Haiku, then to you, when it doesn't. The decider never writes code or prose.

This is what TypeSafe's Jev does natively: it returns typed choices and scores with calibrated probabilities in 70–500 ms, supports up to 255 options per choice, and costs $0.042 per million input tokens with output free ([TypeSafe](https://typesafe.ai/blog/introducing-system-one-models-and-jev), [Cloudflare model page](https://developers.cloudflare.com/ai/models/typesafe/jev/)). Direct access is waitlisted, but it is callable today through two routes:
- **Cloudflare Workers AI:** model `typesafe/jev`, unversioned, zero data retention, 32k context.
- **OpenRouter:** pinned `typesafe/jev-1.13`, through its Decisions API rather than chat completions, zero-retention with `provider.zdr`.

The `~typesafe/jev-latest` alias floats, so it is not used. Vercel AI Gateway also serves it, but no route there is planned.

| Backend | Where | Use |
| --- | --- | --- |
| Jev via Cloudflare Workers AI or OpenRouter | hosted | default decider; for a private repo only the Cloudflare zero-retention route, and only where `repos.yaml` lists it; any route for public ones |
| Claude Haiku with constrained JSON output | hosted | ceiling in the eval, and the escalation target when Jev's confidence is low |
| Liquid AI LFM2.5-1.2B-Instruct with a JSON-schema grammar in llama.cpp, logprobs read for confidence | tower | local fallback when offline, and the eval candidate for "can a small open model match Jev on these questions" |

The decider questions are declared in code (`vigild/decider/questions.py`), one schema shared by all three backends, so swapping backends is config. Questions at launch: `tier` (choice), `size` (XS–L), `value` and `risk` (scores 1–5), `touches_money_path` (bool, used only as a second check behind the path rules, never instead of them), `worth_proposing` (bool), `needs_escalation` (bool, on a review's findings), `duplicate_of` (choice over open issue ids, capped at 255).

Thresholds start at 0.85 to act and 0.6 to escalate to Haiku; below 0.6 the task waits for you. The eval measures each backend's agreement with your own labels on 30 past issues, and the thresholds are tuned from that, not guessed.

One repo covers both public and private targets. `vigil` is a single private repo. The decider route **and the model backends** are chosen per target repo in `configs/repos.yaml`, and `vigild` refuses to send a private repo's text to a backend or route not listed for it. For a private repo the list may name local backends and `jev-cloudflare` only; vigild refuses to start with any other hosted entry there.

**Private repos never go to a hosted API**, with one exception: Jev over Cloudflare zero-retention, and only where a repo lists it. **Owner decision 2026-10-22 (Q2):** `thomasbardhi01/vigil` lists `anthropic`, for self-maintenance, and no other private repo may; the exception is hard-coded in `vigild/repos.py`, not configurable.

```yaml
repos:
  thomasbardhi01/parkagent:
    visibility: public
    decider: [jev-openrouter, haiku, lfm-tower]
    worker_overflow: [runpod]          # hosted eval/overflow allowed
  thomasbardhi01/hearth:               # example only; no repos after ParkAgent yet
    visibility: private
    decider: [lfm-tower, jev-cloudflare-zdr]   # drop the second entry to keep it fully local
    backends: [tower-qwen, tower-fast]
    worker_overflow: []                # tower only
```

## Review tiers and the trust ladder

The end state is that Claude builds and runs end-to-end tests, and the fleet reviews itself. You get there by measuring, not by deciding. A local reviewer earns the right to be the only reviewer for a task shape; it never gets it by default.

| Rung | Who reviews | Who audits | Graduates when |
| --- | --- | --- | --- |
| 0 (build phase) | Claude Sonnet, fresh context | Claude Opus audits 100% of reviews: did the review catch what the audit catches? | n/a; this is where every shape starts |
| 1 | Local reviewer (Qwen3.8-27B or best eval model) **and** Claude Sonnet, both blind | Claude Opus compares the two reviews | local reviewer finds ≥ 90% of Claude's findings and raises ≤ 1 false finding per PR, over 20 PRs of that shape |
| 2 | Local reviewer only | Claude Sonnet audits a 25% sample | sample shows no missed finding of severity ≥ medium over 40 PRs |
| 3 | Local reviewer only | Claude audits a 10% sample, forever | a missed medium finding or any missed high finding drops the shape back to rung 1 |

Shapes graduate independently: docs and dependency bumps will reach rung 3 in weeks; tests-from-spec may take longer; money, auth and consent paths stay at rung 0 permanently. The ladder state lives in `configs/review.yaml` and `vigild` edits it from the audit data; you see every promotion and demotion in the weekly report.

The review contract is the same for every reviewer: input is the diff, the issue, the spec, and the checklist; output is `findings[] {severity, file, line, claim, fix}` plus a verdict. Claude-as-auditor scores a review by matching its findings against the auditor's own, and that match rate is the number the ladder runs on.

What Claude does once shapes graduate: first drafts of features, anything rung 0, end-to-end test authoring and the nightly suite against prod, and the audits above. That is the "rigorous stuff"; everything else is the fleet's.

## The factory floor: jobs as trees, sessions as first-class, specs as the only gate

The end state is a dark factory: you approve specs, Vigil does everything else, and you are paged only when something needs a human. Everything in this section exists to make that safe. The closest public analogue is GitHub's Agent HQ, where a plan is approved before any code is written, agents are managed like team members with their own identity and policies, and a mission-control view tracks every agent across surfaces ([GitHub](https://github.blog/news-insights/company-news/welcome-home-agents/)). Vigil is the self-hosted, model-agnostic version with a harder verification ladder.

**A job is a tree, not a session.** One approved spec creates a job. The job owns a *coord session* (Claude by default), which plans, splits the work into worker tasks with their own contracts, dispatches them, and owns the status issue. Each worker is its own Vigil session in its own container, branch and per-task clone, so it is observable on its own, not hidden inside a parent's context. Workers may spawn subagents (the Agent SDK's Agent tool, OpenCode's subagent action); the SDK does not stream a subagent's internal events ([issue](https://claudeissues.com/issue/43553-feat-propagate-sub-agent-internal-events-in-output-format-stream-json)), so Vigil reads them from the OTel spans, which nest a subagent's model and tool spans under the parent's tool span, plus SubagentStart/Stop hooks. Reviewers and auditors are sessions on the same tree. The UI shows the tree live: job → coord → workers → subagents → reviewer → auditor, each node with its state, what it is doing now, tokens, cost, and a link into its transcript.

```mermaid
flowchart TD
  S[Spec approved by you] --> J[Job pa-0142]
  J --> C[coord session · Claude]
  C --> W1[worker A · tower]
  C --> W2[worker B · claude]
  C --> W3[worker C · tower]
  W2 --> SA[subagent · explore]
  W1 --> PR1[PR] --> R1[reviewer] --> A1[auditor]
  W2 --> PR2[PR] --> R2[reviewer]
  W3 --> PR3[PR] --> R3[reviewer]
  A1 --> M[merge queue]
  R2 --> M
  R3 --> M
  M --> V[post-merge verify: CI + nightly suite]
  V -->|fail| F[fix job · child of pa-0142 · same coord resumed]
  F --> C
```

**Lifecycle, including what happens after you ship.** Job states: `spec_pending` → `approved` → `planning` → `building` → `in_review` → `ready` → `merged` → `verifying` → `done`, with `blocked` and `escalated` reachable from any of them. Merging is not the end. After merge, Vigil watches post-merge CI and the nightly prod suite for a window (default 48 h) and tags every failure with the job fingerprints of the commits involved. A failure, or a bug issue that the decider links to a shipped job, opens a *fix job* as a child of the original: same coord session resumed with its context (SDK sessions are resumable; OpenCode sessions can be forked), the original spec and diffs attached, a new worker tree. Lineage is shown on both jobs. A shipped job that spawns three fix jobs in a week is flagged on the review page; that shape's trust rung drops.

**Every session is a conversation you can read and join.** Vigil proxies each session's transcript into the UI: the Agent SDK message stream for Claude sessions, the v2 `GET /api/event` stream plus the session's message history for OpenCode sessions (run headless, `opencode serve`, one per container; the v2 message endpoint is confirmed in phase 4). The session page shows the conversation as it happens, tool calls inline, and has a send box. A message you send goes into the live session (SDK streaming input; OpenCode v2 `POST /api/session/{id}/prompt`), is logged with `provenance: human`, and shows in the transcript. So you can talk to a coord in the app, nudge a worker, or answer a question a coord raised. Coords raise questions as *inbox items*, never as silent waits.

**Coord backends are pluggable; the coord is the one place to spend quality.** The coord uses the same backend object as workers, so it can be Claude (default, Opus for planning), the tower's llama-server, Ollama on any machine, or OpenRouter. The routing table decides the default per job shape; a local coord is allowed only on shapes at trust rung 3. "Open in Claude Code" on a coord session copies `claude --resume <session_id>` so you can take over in the terminal; the vigil MCP server lets a Claude.ai or Claude Code session dispatch and read jobs; the ParkAgent coord becomes a client of vigild through that MCP server.

**The inbox is the only place you work.** One page, three queues: *Specs to approve* (drafted by a coord from an issue, with the decider's value/risk and the planned worker split), *Ready to merge* (green CI, clean review, audit sample passed, with the diff summary), *Questions and pages* (a coord blocked, a budget hit, a post-merge failure, a demoted shape). Everything else is visible but needs nothing from you. Push notifications for the third queue only.

The inbox is the only surface for **decisions**. `/talk` (the bridge, below) is a conversation surface, not a decision surface:
- Nothing on `/talk` approves a spec, merges, or dispatches.
- A coord that needs a decision files an inbox item, even when the question came up in a conversation.

**Auto-merge is earned per shape, like review.** The merge queue auto-merges only when: the shape is at rung 3, CI green, reviewer clean, auditor sample clean, no money/auth/consent path, and post-merge verification is configured for that repo. Anything else waits in *Ready to merge*. A post-merge failure on an auto-merged PR auto-reverts and opens a fix job. You can turn auto-merge off per repo in one line of `repos.yaml`.

**Talking to coordinators from anywhere: the bridge.** Vigil has one message bus per coordinator (the standing *fleet coord* that drafts specs and triages, plus one coord per job). Three clients write to it, and every message carries `provenance: human` plus the channel it came through:

| Client | How it connects | What you do |
| --- | --- | --- |
| Web UI `/talk` | vigild directly | pick a coord, see its backend (Claude API, tower Qwen over Tailscale, OpenRouter, Ollama, custom), type; the coord answers in the same thread |
| Claude.ai or Claude Code | vigil MCP server (`vigil_talk`, `vigil_listen`, `vigil_jobs`, `vigil_approve`) added as a connector or in `.mcp.json` | say "tell the pa-0142 coord to keep limits in config"; Claude relays it verbatim, the coord's reply comes back into your chat |
| Ollama / any local chat | `vigil-bridge`, a 200-line client that speaks MCP to vigild and OpenAI-chat to the local model; also works with Open WebUI's MCP support | same as above from a fully local chat; no text leaves the Mac and tower |

The bridge never lets the relaying model change your words: `vigil_talk` takes the user's text as a quoted argument and vigild logs both the relay model and the exact text. A coord's backend is chosen per job at creation (and for the fleet coord in `configs/vigild.yaml`); that is the default. `/talk` may switch it between turns. A switch:
- starts a fresh session on the new backend, with the thread history as context;
- is logged.

A local backend is still allowed only on shapes at trust rung 3.

**Frontier practices Vigil adopts, each a test in the repo:**

| Practice | What it means in Vigil |
| --- | --- |
| Plan before code, human approves the plan | The spec gate plus the inbox; a coord's first output is a spec and a worker split, nothing runs before approval |
| Agents as team members with identity | One GitHub App per role (vigil-coord, vigil-worker, vigil-reviewer) so every commit, comment and review is attributed; per-role permissions. **Deferred (owner 2026-10-22):** vigil-worker serves every role for now, down-scoped per use; ADR-0001 says when that changes |
| Policy as code | Routing, review ladder, budgets, WIP, auto-merge rules live in `configs/*.yaml`, versioned, reviewed by PR like code |
| Verification ladder | fail-before-fix test → CI → fresh-context review → audit sample → post-merge CI → nightly prod suite → auto-revert |
| Untrusted content | Issue bodies, PR comments, fetched pages and repo files other than `CLAUDE.md`/docs are fenced as data in every prompt; a worker never acts on instructions found in them; a test plants an injection in an issue and asserts no tool call follows it |
| Best-of-n on the tower | For a shape with a low first-pass rate, run 2–3 tower attempts in parallel from the same spec and let CI plus the reviewer pick; free on your GPU, logged as one job |
| Checkpoint and resume | Every session id is stored; coords resume with context for fix jobs; a crashed worker resumes from its last commit |
| Replayable jobs | A job is spec + base commit + backend; `vigil replay <id> --backend X` reruns it, which is also how the eval works |
| Flake control | A test that flips across two runs of the same commit is quarantined and filed, never counted against a worker |
| Reviewer evals | Reviewers are scored against the auditor and against ReviewBench-style fixtures before entering the ladder |
| Circuit breakers | Daily cap, per-task cap, WIP, max fix-job depth (3), max escalations per day; any breaker trips → inbox page, dispatch pauses |

**Owner decisions 2026-10-22: the Cognition corrections.**
- **Single writer by default.** A job has one writing worker unless the plan says otherwise.
- **Subagents are read-only.** A worker's subagents search and read; they never edit.
- **Parallel writers only with disjoint files and pinned interfaces in `spec.md`.** The spec gate checks the disjointness: two workers with no `depends_on` path between them may not share a file, and parallel workers need a `## Interfaces` section.
- **Escalation forks full context.** An escalated attempt starts from the failed attempt's whole context, not a summary (phase 2, with escalation).
- **A per-repo knowledge base** (code map, lessons store, search subagent): phase 2.
- **A dev image per repo, built by vigild from the repo's lockfiles when they change and validated by running its suites offline, with dispatch refused on a stale or failed image: phase 1** (owner direction, review round 2; this replaced the nightly validation).
- **KPIs drive the ladder, routing and the cost model:** first-pass merge rate, findings per PR by severity, revert + fix-job rate, cost per merged PR, median spec→merge. They are defined now and their raw data is stored now; each is computed where data exists.
- **`ask_coord` in the worker packet: phase 1.** A blocked worker asks its coord instead of guessing; the question also lands in your inbox.

**Owner decisions 2026-10-22: the bridge's phasing.** The bus, the MCP tools (`vigil_talk`, `vigil_listen`) and `vigil-bridge` are phase 2. The `/talk` page is phase 3.

## Job execution protocol: stages, artifacts, acceptance, prompting

This is how a job runs, written the way a release process is written at a company that guards main: every stage has inputs, an artifact it must produce, an acceptance check a machine runs, and an owner who moves it on. Nothing moves on a feeling.

**How workers are housed.** Three session kinds, one contract. A *coord* is a Claude Agent SDK process (Opus) in its own container, long-lived for the job, resumable. A *Claude worker* is an Agent SDK process (Sonnet by default) in a fresh container with one clone on one branch; it is not an OpenCode session, because the SDK gives hooks, permission modes and OTel for free. A *local worker* is a headless `opencode serve` in a fresh container pointed at the tower (or OpenRouter, or any endpoint), driven over HTTP. Reviewers and auditors are Claude workers with `edit: deny`. Containers are the unit of isolation and of cost; a session never outlives its stage.

**Stages.** Each row is a gate. The artifact is a file committed on the job's branch under `.vigil/` or a record in vigild; the acceptance check is code, not judgment. Text marked **Learning (planned)** belongs to the learning epic ("Learning loop", below). It is not current behaviour. A *see Open N* points to the unresolved conflict, defined in that section.

| Stage | Input | Owner | Artifact produced | Acceptance (machine-checked) | On failure |
| --- | --- | --- | --- | --- | --- |
| 0 Intake | issue, label, or `vigil run` text; your rough ideas | decider | `intake.json`: value, risk, size, touches\_money, duplicate\_of, confidences | confidences above act threshold; not a duplicate; WIP free | wait in inbox or page you |
| 1 Spec | intake + repo docs + your notes | coord (Claude). **Learning (planned):** a separate fresh-context session writes the hidden tests from the spec and the product model only; it is never the worker's session | `spec.md`: goal, non-goals, acceptance criteria as testable statements, `test_cmd` per criterion, files in/out of scope, money paths, interfaces (when workers run in parallel), risks, rollback; `plan.md`: worker split with a contract per worker, dependency order, estimated size, each criterion's verification level. **Learning (planned):** `hidden_tests` (a vigild record, never on the branch); `reward_schema` (which graded signals apply to the shape); a difficulty estimate; `plan.md` must cite the product-model invariants each worker touches; the citations are the retrieval key for matching recovery playbooks into the packet | spec gate: every criterion has a test command; every worker contract validates against the schema; money paths flagged match the path rules; parallel workers have disjoint files and pinned interfaces; every criterion has a verify entry, and one left at level 0 says why. **Learning (planned):** the hidden tests fail on base; every plan entry that touches a product-model invariant cites it, and every citation resolves (*see Open 1, 13, 14, 15*) | coord revises up to 2×, then pages you |
| 2 Approve | spec + plan | you (inbox) | approval record with your edits | signed by you | job stays `spec_pending` |
| 3 Build | one worker contract | worker, then vigild | branch with commits; `worker_report.json`: test before/after, files changed, summary, self-check answers, open questions; a draft PR that **vigild** pushes and opens from the host (P2, owner 2026-10-22), never the worker | `must_fail_first` observed; `test_cmd` exits 0; files changed ⊆ scope; no test weakened (diff of test files reviewed by a check); lint/types clean; turns and cost under limits; all checked on the host before the push | retry with findings appended (attempt 2), then escalate tier. **Learning (planned):** the attempt protocol. There are up to N attempts (N = 2 to start). Between attempts the repo is reset, and the worker gets its failed patch plus category-level verifier feedback. Each attempt is its own episode span with its own reward (*see Open 2*) |
| 4 Verify | branch | CI (GitHub Actions). **Learning (planned):** vigild's verifier, as the reward computer | CI run; coverage delta; flake status. **Learning (planned):** `reward.json`, signed by vigild. It holds the gates, the mutation score (mutmut, Stryker, cargo-mutants) and the coverage delta on changed lines only, property and stateful results, a visual diff where the shape needs one, the cost tie-break, and hidden-test results by category | all required checks green on a clean runner, not the worker's shell; flaky tests quarantined, not counted. **Learning (planned):** the reward is computed only in the verifier, from a clean checkout of head; the worker's own claim is logged at zero weight; the reward's inputs are hashed onto the Phoenix span (*see Open 10, 11, 14, 16*) | back to build with the CI log attached. **Learning (planned):** the worker sees only the visible tests plus category-level feedback ("hidden 3/7 pass; failing: concurrency, empty-input") |
| 5 Review | PR diff + issue + spec. **Learning (planned):** plus `reward.json`, with the reviewer told to find what it missed (*see Open 17*) | reviewer (fresh context) | `review.json`: findings\[\] {severity, file, line, claim, fix}, verdict. **Learning (planned):** each finding labelled (type, severity), and later tagged real or false by audit | zero findings of severity ≥ medium; checklist complete | findings become a fix task on the same branch; two rounds, then escalate |
| 6 Audit | review + diff | auditor (Opus, sampled by rung). **Learning (planned):** it samples top-reward jobs as well as random ones (*see Open 12*) | `audit.json`: findings the reviewer missed, match rate. **Learning (planned):** real/false tags on review findings; reward–audit agreement per component | match rate feeds the trust ladder; no missed high finding. **Learning (planned):** a reward component whose agreement falls below its threshold is frozen out of the reward until recalibrated | demote shape; findings become a fix task |
| 7 Integrate | all worker PRs for the job | coord | integration branch, integration test run, PR stack order. **Learning (planned):** the merge record stores the reward, the model and its version, and the harness config hash (the spec puts it here; the merge itself is stage 8: *see Open 18*) | integration tests pass; stacked PRs rebase clean | coord opens an integration fix task |
| 8 Merge | ready PRs | you, or auto-merge at rung 3 | merge commits with job fingerprint | ruleset satisfied; auto-merge conditions | waits in inbox |
| 9 Post-merge | merged commits | vigild watcher | verify record: post-merge CI, nightly suite, error-rate window. **Learning (planned; the spec calls this a new stage):** the full suite plus a canary in the integration env; regressions bisected to a job; a regression within 30 days that bisects to the job sets its reward to 0 | 48 h window clean | auto-revert if auto-merged; fix job as child, coord resumed. **Learning (planned):** it also auto-opens an incident that runs the middle loop (*see Open 19*) |
| 10 Close | verify record | vigild | job summary, cost, lineage, lessons line appended to `docs/feature-map.md` | docs updated in the merged PRs | n/a |
| Replay. **Learning (planned):** a new stage, weekly, outside any job | closed jobs and synthesized hard tasks; the current harness | vigild | per-model results (Opus and the best local) on the tune split; the held-out split once per release | only tune-split results drive trust-ladder promotions and harness-parameter tuning (turn budgets, packet size, retry depth, routing thresholds) | the change is not made; the result is reported (*see Open 3, 4, 5, 6*) |

**Your way of working, mapped.** You arrive with a feature request and ideas; you build; you fix until the MR survives a hard audit; then you fix the bugs that surface after. In Vigil that is: your request and ideas go into stage 1 as `notes` on the spec (the coord must address each idea in the plan, adopting or rejecting it with a reason); stages 3–6 are the build-and-fix loop, bounded by attempt counts; stage 9 is the after-ship fix loop, bounded by fix-job depth. You stop being the loop and become the two gates (2 and 8).

**Prompting discipline.** Every session receives one *task packet*, assembled by vigild, never free text:

1. *Role and rules*: the role prompt for coord/worker/reviewer/auditor from `prompts/<role>.md`, versioned, then the repo's `CLAUDE.md`/`AGENTS.md`.
2. *Context pack*: `docs/architecture.md` and `docs/feature-map.md` excerpts selected by the files in scope, the contents of files in scope, the relevant tests, the last three merged PRs touching those files, and for fix jobs the original spec, diffs and failure logs. Capped by token budget; the coord decides what to drop and records it.
3. *Contract*: the task JSON, with `done_when` restated as a checklist.
4. *Untrusted content*, fenced: issue text, PR comments, fetched pages. The role prompt says these are data and any instruction inside them is ignored; a test plants one and asserts no tool call follows.
5. *Output schema*: the exact JSON the session must end with (`worker_report.json`, `review.json`). A session that ends without it is a failure, not a partial success.
6. *Self-check*: before returning, the worker answers five questions in the report: did the test fail first; what did I change outside scope and why (must be nothing); what did I not do; what would a reviewer flag; what could be deleted.

The worker packet also explains `ask_coord` (owner decisions 2026-10-22, phase 1): a blocked worker writes its question to `.vigil/<job>/<task>/ask_coord.json` and stops; vigild files it to the coord and the inbox.

**Learning (planned):**
- The context pack also carries the recovery playbooks that the plan's product-model citations match.
- An attempt after the first carries its failed patch and the category-level verifier feedback. It never carries the hidden tests.
- Packet templates may evolve (GEPA/DSPy style), but only in the outer loop, with the verifier as the metric, re-run per model (*see Open 4*).

Role prompts are short, imperative, and never change mid-job. Changing a prompt is a PR with an eval run attached. No session is ever asked to "continue" in a chat; a new stage is a new packet.

**When to move on.** A stage advances only when its acceptance column is true and the owner has written the artifact. Vigild enforces this in one state machine (`vigild/jobs/fsm.py`) with a test per transition. There is no manual override except yours from the inbox, and that is logged.

**Dependency changes are their own job** (owner direction, 2026-10-22). A worker never adds a dependency: verification runs the base's runner files on images built from the base's lockfiles, so a dependency change can't pass. Instead:
1. The worker's task blocks with "needs dep <name>@<version>": it reports `needs_deps: [{ecosystem, package, version, why}]` in `worker_report.json`, or vigild reads it from a manifest or lockfile the worker changed. The inbox item shows the requested deps and the worker's manifest and lockfile diff, fenced as untrusted.
2. vigild opens a child **deps job** (shape `deps`; the blocked task's `blocked_on`) that changes only the manifest and the lockfile. It runs **no model**: vigild runs the package manager's lockfile-only update (`pnpm add <pkg>@<ver> --lockfile-only`, `uv add …`) in a dedicated build container with registry egress and nothing else: no repo secrets, no gateway token, no model. A diff that touches anything but manifests and lockfiles fails the job.
3. **Pins:** the requested version must be a release at least 14 days old by the registry's publish time, and so must every other package the lockfile pulls in, or the job fails with the list.
4. **Gates:** the dev image is rebuilt from the new lockfile, the full suite runs offline on base plus the new lockfile, and vigild opens the PR. `deps` starts at rung 1 and always needs your review, like money paths.
5. After that PR merges, the dev image key changes, and the original task resumes on the new image.
6. Dependabot-style bumps come in the same way: a scheduled intake, or a Dependabot PR adopted as a deps job.

Phase 1 builds the block, the inbox item and the job link in the state machine (you resume from the inbox); phase 2 the deps job and auto-resume; phase 6 the Dependabot intake.

## Documentation standard: context baked into every job

The rule: **the repo is the source of truth, every job reads from it through one assembled packet, and every job writes back to it before it closes.** Nothing a job learns lives only in a chat. This follows Anthropic's guidance (curate what the model sees and when; keep `CLAUDE.md` light and about real gotchas; load reference material progressively as a tree, not one giant file; use dedicated skills for verification and review; prefer the model's judgment to long rulebooks) and Cognition's (share full context, keep writes single-threaded, review with a clean context, invest in context engineering rather than prompt tricks).

**Is a graph the right store?** No, not as something you maintain. The repo already is a graph: files, symbols, imports, routes, tests and PRs are nodes and edges, and git keeps them current for free. A separate knowledge graph is a second copy that drifts the first week, which is the same reason ParkAgent chose Mermaid-in-Markdown over Figma. The X conversation about graphs is mostly about memory for chat agents, not about codebases. So: *derive* graph views when a packet is assembled (a code map with symbols and edges, regenerated per commit and cached), *maintain* only plain text that humans and agents both read, and *query* the derived map through a read-only search subagent. Mermaid stays the drawing format because it is text, reviewable in the same PR, and renders on GitHub and in the UI.

**The documentation tree.** Eight layers, each with one owner, one update trigger and one CI check. ParkAgent already has layers 1–3; Vigil adds the rest.

| # | Layer | Holds | Owner · when written | Check |
| --- | --- | --- | --- | --- |
| 1 | `CLAUDE.md` = `AGENTS.md` | rules, real gotchas, how to run tests; ≤ 200 lines | you; by PR | lint: length, no duplicated facts from layers 2–3, `claude doctor` clean |
| 2 | `docs/architecture.md` | the system's flows as Mermaid: money path, state machine, job tree, deploy | the PR that changes a flow | CI: Mermaid parses; a PR touching a flow's files without touching this file fails |
| 3 | `docs/feature-map.md` + `.mmd` | every route, job, screen and feature with `file:line` evidence | generated by the code-map agent, committed | CI: fails if a route or job in code is missing from the map |
| 4 | `docs/decisions/ADR-NNNN.md` | one page per architectural decision: context, options, choice, consequences | coord, when a plan changes architecture; you, when you overrule | PR template requires an ADR link for `architecture` labels |
| 5 | `docs/knowledge/lessons.jsonl` | one line per closed job: what the reviewer caught, what a fix job revealed, what the next job should know; tagged by path and shape | vigild at job close, from `close.md` | nightly audit prunes lines older than 90 days unless referenced |
| 6 | `.vigil/<job>/` on the branch | `intake.json`, `spec.md`, `plan.md`, `worker_report.json`, `review.json`, `audit.json`, `close.md` | each stage's owner | the stage gate: missing or invalid artifact = stage not done |
| 7 | code map (derived, not committed) | symbols, imports, callers, tests per file, coverage, churn; built per commit, cached | vigild, on packet assembly | rebuilt if the cache's commit ≠ HEAD |
| 8 | claude.ai project | handoffs, research, reviews of reviews: only what does not belong in the repo | you and your Claude sessions | none; it is a notebook, never a dependency of a job |

**How a packet is assembled from the tree** (progressive disclosure, with a token budget per layer, dropped items logged in `plan.md`):

1. Always: layer 1 whole; the role prompt.
2. By files in scope: the layer-2 diagrams whose node names match those files; the layer-3 rows for those files; layer-7 symbols, callers and tests for those files.
3. By search: layer-5 lessons tagged with those paths or that shape; layer-4 ADRs those files reference.
4. For fix jobs: the parent's layer-6 artifacts and the failure logs.
5. On demand: a read-only search subagent over the whole repo and the code map, so a worker never guesses where something lives.

**What each stage writes back.** Spec and plan write layer 6; a plan that changes a flow also writes layer 2 and an ADR; build writes layer 6 and any layer-2/3 updates the spec requires (checked, not trusted); close writes `close.md`, which vigild distils into one layer-5 line and, if the job shipped a new route or job, re-runs the code-map agent for layer 3. The UI's job page links every layer-6 artifact, and the session packet is downloadable, so you can see at any moment exactly what a worker was given.

**Freshness.** Drift is caught by machines, not memory: the layer-3 CI check, the layer-2 "docs in the same PR" check, a nightly audit that diffs layer 1 against layers 2–3 for duplicated or contradicting facts, and a staleness score per file on the dashboard (days since a doc mentioning it was updated versus days since the file changed). A repo whose staleness score crosses a threshold gets a docs job proposed to the inbox.

**Owner decisions 2026-10-22: the documentation standard's phasing.**
- **Phase 1:** layers 1, 2, 3 and 6; the packet assembler with per-layer token budgets and a dropped-items log; the "docs in the same PR" check; the missing-route check.
- **Phase 2:** layers 4, 5 and 7, the search subagent, and the staleness score.
- **Layer 8 is never read by a job.**
- The tree applies to the vigil repo from the phase 1 PR onward.

## Verification ladder (owner decisions 2026-10-22)

How far each acceptance criterion's check reaches. Levels 0, 1 and 3 only, for now:
- **Level 0:** example tests that fail first. **Phase 1:** a `verify` field in the spec schema, at level 0.
- **Level 1 (phase 2):** properties derived from the criteria:
  - Hypothesis for Python;
  - SwiftCheck or parameterised swift-testing for Swift;
  - Schemathesis for OpenAPI routes;
  - Hypothesis stateful tests for vigild's job FSM.
- **Level 3:** models of ParkAgent's payment state machine and vigild's FSM. You write them with Opus, agents maintain them, CI checks them. Scheduled after phase 2.
- **The coord must state in `plan.md` any criterion it couldn't raise above level 0, and why.** The plan schema enforces this.
- **Levels 2, 4 and 5** are documented as later options, not built.
- **Audit sampling and ladder promotion weight criteria by verify level:** a shape whose criteria sit at level 0 is sampled harder and promoted slower than one at level 1 or 3. Documented now; the code is phase 2.

## Learning loop (owner spec, 2026-10-23)

How Vigil gets better from its own jobs.
- **Design only.** Nothing in this section is built. It is the **learning epic**, after phase 5: `docs/feature-map.md` "Learning", `docs/learning/README.md`, and the first three experiments, #9, #10 and #11.
- **The stage changes** are written into the job execution protocol, marked **Learning (planned)**.
- **Conflicts.** Where this contradicts the current design, an *Open N* note says so. Each note is defined once, in reading order, and the protocol table points back to it. The owner decides each one; nothing here overrides the sections above until then.

**Principle.** Vigil learns in the harness, not the weights. The model is a swappable executor.
- **Where learning lands:** in artifacts that a new model can read:
  - hidden acceptance tests;
  - graded verifier rewards;
  - recovery playbooks;
  - product-model deltas;
  - a replay eval set.
- **Weight training:** only on the local tier, offline, from Vigil's own verifier-labelled trajectories.
- **Rejected: co-evolving harness and policy** (HarnessForge style). It couples the harness to one model, which is what breaks under a swap, and it needs harness changes to be self-graded.
- *Open 1:* "product model" is defined nowhere in this design, yet the spec uses it throughout:
  - hidden tests are written from it;
  - plans must cite its invariants;
  - a reward gate checks contradictions against it;
  - the middle loop writes product-model PRs.

  The closest things are layers 2–4 of the documentation tree and the level-3 models of the verification ladder. A self-generated product-model PR also meets the intake rules: self-generated work needs `value ≥ 3`, and "nothing self-generated touches a money, auth or consent path". Owner to decide what the product model is, where it lives and who writes it.

**Three loops, plus the tower.**
- **Inner, per job: the attempt protocol (FC-SWE), no training.**
  - Up to N attempts, starting at N = 2.
  - Between attempts, vigild resets the repo and gives the worker its failed patch plus category-level verifier feedback.
  - Each attempt is its own episode span with its own reward, so credit never leaks from a recovery to a failed first attempt.
  - *Open 2:* contradicts stage 3 and the current retry rules:
    - **Automatic retry.** Stage 3's "retry with findings appended (attempt 2), then escalate tier" and architecture B35 have a failed worker block the job with one inbox item, and **the owner** retries. The protocol retries automatically.
    - **Reset against fork.** "Escalation forks full context" starts an escalated attempt from the failed attempt's whole context. The protocol resets the repo and passes only the patch and feedback.
    - **Parallel against sequential.** Best-of-n on the tower runs 2–3 attempts in parallel; the protocol runs them one after another.
    - **One cap.** B35 gives a retry only what's left of the task's $2 cap, so N attempts share one $2 cap.
    - **The counter.** The contract's `attempt` field already counts escalation steps: routing rule 5 fires at `attempt ≥ 3`, and the escalation ladder sends attempt 3 to Sonnet. Protocol attempts would collide with it.

    Owner to decide.
- **Middle, per incident: PROBE's shape.**
  - The flow: a telemetry bundle from the Phoenix spans → a structured diagnosis (anchor, primary cause, behavioural mistake, evidence, bounded confidence) → a gated hint.
  - Every hint target traces to evidence. A hint has a target, an operation, a verification signal and a boundary.
  - An infrastructure failure is never turned into a corrective hint.
  - The result is stored as a recovery playbook and an eval case. Where the cause was a wrong assumption, it is also a product-model PR (see Open 1).
- **Outer, weekly and per release: replay.**
  - Closed jobs and synthesized hard tasks are replayed against the current harness with two models: Opus and the best local.
  - Replay drives trust-ladder promotions and harness-parameter tuning (turn budgets, packet size, retry depth, routing thresholds), on the tune split only.
  - The held-out split is reported once per release.
  - *Open 3:* contradicts phase 7, "Eval", and the "Eval protocol". Those replay 10 closed issues a quarter ("Ten replayed issues per quarter decide which task shapes the tower keeps"), score them by CI, the reviewer and "would a human merge this unchanged", decide by ≥ 70% first-try CI and ≤ 1 finding, and come after phase 6. The outer loop is weekly, scored by the reward, decided on a tune split, and comes after phase 5. Owner to decide whether the epic absorbs phase 7 or builds on it.
  - *Open 4:* contradicts the trust ladder and policy as code.
    - **A second promotion path.** The ladder promotes a shape only from live audit match rates (20 PRs at rung 1, 40 at rung 2). It keeps money, auth and consent "at rung 0 permanently", and starts `deps` at rung 1 with the owner always reviewing. Replay results aren't audits of live reviews, so promotion from replay is a second path, and it must never touch those shapes.
    - **Automatic policy changes.** Harness-parameter tuning and packet-template evolution change config and prompts. Under policy as code, config changes only "reviewed by PR like code", and "changing a prompt is a PR with an eval run attached".

    Owner to decide.
  - *Open 5:* contradicts the budget caps: $5 a day, $2 a task, $100 a month, all hard stops.
    - **Daily and monthly caps.** A weekly replay of even 20 tasks with Opus, at up to $2 each, is up to about $40 a week. That is eight days of the daily cap and about 40% of the monthly cap, before any live job.
    - **The task cap.** The $2 cap truncates Opus and Sonnet runs (the design's cost model puts a Sonnet S/M task near $3), which biases the swap gap toward zero.
    - **The experiments.** The spec's "a few dollars" holds for #9 (about $3–8), not for #10 (about $6–85) or #11 (about $60–80).

    The precedent is #8, whose bench has its own budget that "never draws from the daily job cap", and phase 7's "one-time eval budget". Either is a new cap the gateway would enforce. Owner to decide.
  - *Open 6:* contradicts owner-only approval. "Inbox approval by the owner is the only exit from `spec_pending`", and replayed and synthesized jobs have no owner approval. #8 handles this with an automatic, labelled approval for bench tasks, which is a new exit. Owner to decide.
- **Tower, later and optional: a rejection-sampling fine-tune.**
  - An 8B-class local model, trained with QLoRA from verifier-passing trajectories.
  - GRPO is out of scope on a 16 GB card.
  - Not scheduled until the middle loop has produced a few hundred passing trajectories.
  - *Open 7:* contradicts "Claude outputs never enter a training set" and the provenance rule.
    - **Mixed trajectories.** A `local` trajectory's packet carries Claude-written text: the spec, the plan, hidden-test feedback and playbooks. Exporting the trajectory exports that text.
    - **No provenance on spans.** Phoenix, the proposed episode store, keeps spans with no provenance tag and no CHECK constraint, while every stored artifact must carry one.

    Owner to decide.
  - *Open 8:* contradicts the training-data firewall's label rule. Fine-tuning is "allowed only on its own attempts graded by your test suite". Hidden tests are written by a Claude session (provenance `claude`), so a trajectory that is labelled passing because of hidden tests isn't graded by the repo's own suite. Owner to decide.

**Reward: lexicographic, never a weighted sum.**
1. **Gates; any failure scores 0:**
   - the hidden tests pass;
   - the executed-test set is a superset of base's (the verifier's rule today);
   - no blocklist or harness tampering;
   - the product-model contradiction check passes;
   - the visual baseline is within tolerance, where the shape requires one.
2. **Quality, only if every gate passes:**
   - the mutation score on changed lines;
   - the coverage delta on changed lines only, never global;
   - property and stateful test results;
   - reviewer findings by severity.
3. **Cost, as the tie-break:** tokens, wall-clock, GPU-hours, fix-job depth.
4. **Retroactive:** a regression within 30 days that bisects to the job sets its reward to 0.

*Open 9:* the quality tier overlaps the verification ladder. Property and stateful tests are already verification level 1 (phase 2: Hypothesis, SwiftCheck, Schemathesis, and Hypothesis stateful tests for the FSM). The reward should read level 1's results rather than add a second property-test mechanism. Owner to decide.

How the reward is computed and checked:
- It is computed only in the verifier container, from a clean checkout of head.
- The worker's own claimed result is logged at zero weight.
- The reward's inputs are hashed onto the Phoenix span.
- Audit deliberately samples top-reward jobs as well as random ones. Reward–audit agreement is tracked per component, and a component below its threshold is frozen out of the reward until it is recalibrated.

Where it conflicts:
- *Open 10:* contradicts architecture B33. The verifier container runs head's code, and B33 says that code can forge its report ("the report is evidence of what ran, not a defence against a hostile test"), which is why vigild decides the verdict on the host. A reward computed inside the container can be forged by the code it judges. A key to sign `reward.json` can't be in a container either ("No secret in … a container"). The likely reading is that containers emit raw reports and vigild computes and signs `reward.json` on the host. Owner to decide.
- *Open 11:* the verifier container can't see every reward input. The cost tie-break comes from the gateway's metering, and reviewer findings come from Review, both of them in vigild. The container has no network. "Computed only in the verifier container" can hold for the gates and the test-derived quality signals, not for reviewer findings or the cost tier. Owner to decide.
- *Open 12:* there would be three audit sampling rules: the ladder's random sample by rung (25%, 10%), the verification ladder's "audit sampling … weight criteria by verify level", and now top-reward jobs. The ladder's missed-finding statistics are only valid on the random sample, so they must be computed from it alone. Owner to decide.

**Hidden tests.**
- **Who writes them:** at spec time, a separate fresh-context session writes acceptance tests from the spec and the product model only. The spec author's session is never the worker's session.
- **They must fail on base.**
- **Where they live:** vigild stores them, and they enter only the verifier container.
- **What the worker sees:** only the visible tests, plus category-level feedback ("hidden 3/7 pass; failing: concurrency, empty-input").
- *Open 13:* contradicts stage 1 and stage 2 as they stand.
  - **Who writes tests.** Today the coord writes a `test_cmd` per criterion and the worker writes the failing tests. Hidden tests add a second test author at spec time.
  - **Where artifacts live.** Every stage-1 artifact is in layer 6, `.vigil/<job>/` on the job's branch, which the worker clones. Hidden tests would be the first vigild-only artifact.
  - **What the owner sees.** The inbox shows the owner each worker's `test_cmd` before approval. It is undecided whether the owner approves tests the worker never sees.

  Owner to decide.
- *Open 14:* contradicts the verifier's run set (architecture B33). The verifier runs only the approved spec's `test_cmd`, three times (base; before, which is the base plus the worker's tests; after, which is head), with the base's runner files. The superset check and must-fail-first ("one of the worker's own new or changed tests fails before and passes after") are computed over that set. Hidden tests need runs of their own: on base for "must fail on base" and on head for the gate. Adding them to the same runs changes the executed-set comparison. Owner to decide.
- *Open 15:* contradicts the private-repo rule. A private repo's text goes only to local backends (and `jev-cloudflare`), with `anthropic` allowed for vigil alone. The following can't run for any other private repo, so there the author must be local or there are no hidden tests:
  - an Opus hidden-test author;
  - a Claude diagnosis in the middle loop;
  - an Opus replay;
  - LLM-driven task synthesis.

  Owner to decide.

**Phase changes.** Each one is in the job execution protocol's table, at its stage:
- **Spec:** new artifacts: `hidden_tests`, `reward_schema` (which graded signals apply per shape), and a difficulty estimate.
- **Plan:** must cite the product-model invariants it touches. The citations are the retrieval key for matching recovery playbooks into the packet. The planned spec gate refuses a plan entry that touches an invariant without citing it, and a citation that doesn't resolve.
- **Build:** the attempt protocol above, with episode and attempt spans.
- **Verify:** becomes the reward computer. It adds:
  - mutation testing: mutmut for Python, Stryker for TypeScript, cargo-mutants for Rust;
  - changed-line coverage;
  - property tests;
  - visual diff;
  - `reward.json`, signed by vigild.
  - *Open 16:* contradicts the verifier's image and limits:
    - **Tools.** The verifier uses the repo's plain dev image, built from the repo's lockfiles only, and mutmut, Stryker and cargo-mutants aren't in any of them. Adding a tool to the target repo is a deps job that changes the repo; adding it to the image makes a second image definition.
    - **Network.** The verifier has no network, but SWE-smith's LM-driven strategies and Change2Task need a model.
    - **Time.** Mutation runs multiply suite time, while the verifier allows one container at a time per machine and 15 minutes per run, shared with live jobs.

    Owner to decide.
- **Review:** reviewers receive `reward.json` and are told to find what it missed. Findings are labelled (type, severity), and audit later tags them real or false.
  - *Open 17:* contradicts the order of the stages and the review contract.
    - **Order.** `reward.json` is emitted at Verify, before Review exists, so it can't hold the "reviewer findings by severity" component unless it is recomputed after Review.
    - **What a reviewer sees.** The review contract gives every reviewer "the diff, the issue, the spec, and the checklist", and rung 1 runs two reviewers "both blind". A reviewer told to find "what reward.json missed" skips what it caught. The auditor then counts those as missed, so match rates, the ladder's currency, fall for reasons unrelated to the reviewer.
    - **A tension, not a contradiction:** the spec keeps "LLM-as-judge" out of the reward but allows it "as reviewer", and reviewer findings feed the reward.

    Owner to decide.
- **Audit:** samples top-reward jobs as well as random ones (see Open 12).
- **Integrate:** the merge record stores the reward, the model and its version, and the harness config hash.
  - *Open 18:* the spec puts this at "Integrate", which is stage 7: the coord's integration branch and stacked PRs. The merge record itself is made at stage 8, "Merge". The protocol table writes it at stage 7, as the spec says, and notes stage 8. Owner to decide which stage carries it.
- **Post-merge** (a new stage in the spec):
  - the full suite plus a canary in the integration env;
  - regressions bisected to a job;
  - retroactive reward;
  - an incident opened automatically, which runs the middle loop.
  - *Open 19:* contradicts stage 9 and phase 6, which already define post-merge: the watcher on "post-merge CI, nightly suite, error-rate window", with a "48 h window clean".
    - **The window.** The 30-day retroactive window is not the 48 h one. A job reaches the terminal `done` after 48 h, so its reward would change after `done`, outside the job FSM.
    - **The canary.** "A canary in the integration env" names an environment this design doesn't define.
    - **The incident.** It runs a paid model session without the owner. That sits against "Audits produce findings, not tasks", even though stage 9 already opens fix jobs automatically.

    Owner to decide.
- **Replay:** a new stage, weekly, as described under the outer loop.

**Measurement.** Three pools, each with a frozen 20% held-out:
- merged-PR replay from our repos;
- synthesized hard tasks from our repos: SWE-smith bug injection and Change2Task merged-change conversion, with the original tests as hidden tests;
- a 50-task seeded SWE-bench Pro public sample, as an external anchor only.

**The anchor and #8, reconciled.** #8 ("vigil bench") already covers this sample. The proposal, recorded in #8 for the owner to confirm:
- #8 draws more than 50 tasks; it already says 50–100.
- Tasks 1–50 in seed order are the learning anchor, and nothing ever tunes on them.
- #8's tune split comes only from tasks 51 onward.

Metrics, in priority order:
1. **Durable-fix rate:** the hidden tests pass and there is no attributed regression at 30 days.
2. **Cost per durable fix,** by shape and route.
3. **Swap gap:** Opus minus the best local, on the same harness. A widening gap means the harness got model-specific.
4. **Human interventions** per merged PR.
5. **Reward–audit agreement** per component.
6. **Harness lift:** the Vigil pipeline minus a mini-swe-agent baseline, on the same model and tasks.

"Near frontier" is claimed only when the first two improve on held-out across two consecutive releases while the public anchor does not widen.
- *Open 20:* two loose ends in the spec's measurement:
  - a frozen 20% held-out is redundant for the anchor, which is never tuned on;
  - "the public anchor does not widen" names no gap.

  Owner to decide which gap; the swap gap measured on the anchor is the likely one.

**Borrow and keep out.**
- **Borrow:**
  - SWE-smith and Change2Task for task synthesis, inside the verifier container (see Open 16);
  - mini-swe-agent as the baseline executor in replay;
  - Harbor as the runner for the public sample (#8 already evaluates it first);
  - PROBE's three layers for incidents;
  - FC-SWE's attempt protocol;
  - GEPA/DSPy-style prompt evolution, for packet templates only, with the verifier as the metric, only in the outer loop, re-run per model (see Open 4);
  - Phoenix as the episode store, with new `episode`, `attempt` and `reward` span kinds.
- **Keep out:**
  - OpenHands as a runtime (a second orchestrator);
  - LLM-as-judge as a reward component (fine as a reviewer);
  - anything that puts the reward function, the hidden tests or the mutation config inside the worker container;
  - self-refine within one session;
  - full co-evolution.
- **Every tool named here is planned, not adopted:** mutmut, Stryker, cargo-mutants, SWE-smith, Change2Task, mini-swe-agent, Harbor, GEPA/DSPy. When one is adopted, its PR gives it an exact pin at least two weeks old, recorded in `docs/sources.md`, and a one-line reason (the no-new-dependency rule).
- *Open 21:* contradicts "one way to do each thing" and "one task contract for every executor". mini-swe-agent is a third agent loop, and Harbor is a second runner of agent jobs beside vigild. Each would need a contract adapter, and the gateway for every model call ("Nothing else holds a provider key"). Owner to decide.

**The epic.** It sits after phase 5 (feature map "Learning"). Its first three items each run on data from the first ParkAgent run, for a few dollars (see Open 5 for what they actually cost). They run only through vigild, after the owner confirms; no session touches ParkAgent directly.
1. **Hidden-test validity (#9).** For 20 closed ParkAgent jobs, a fresh Opus session generates hidden tests from the spec plus the product model, and they run against the merged fix. Below ~80% pass, the spec stage is the bottleneck and nothing else proceeds first.
2. **Reward validity (#10).** Compute the full reward on 30 merged and 30 rejected historical PRs. Report each component's AUROC against the human outcome, and drop components under ~0.7.
3. **Swap gap (#11).** A 20-task replay on the same harness: Opus vs Sonnet vs Qwen3.6 on the tower. It needs phase 5. If the local model is under ~20% where Opus is over 60%, move the routing boundary toward Sonnet and defer tower training.
   - *Open 22:* the executor is a confound in #11. Today Claude models run in the Claude executor and the tower's models run in OpenCode, so "the same harness" (Vigil's packets, verifier and prompts) still differs in the agent loop. The gap would then measure executor plus model unless all three models run in one executor. A minor point: `configs/repos.yaml` lists no local backend for ParkAgent, so the Qwen runs need a config change. Owner to decide.

## Tower: local model serving

Hardware: Linux, RTX 5070 Ti (16 GB GDDR7), 32 GB DDR5. The right shape is a mixture-of-experts model with experts offloaded to system RAM, served by llama.cpp, which has `--n-cpu-moe` and `--cpu-moe` for exactly this ([llama-server manual](https://manpages.debian.org/unstable/llama.cpp-tools/llama-server.1.en.html)). vLLM needs the whole model in VRAM, so it is not used here.

| Model | Fit | Role | Serve on |
| --- | --- | --- | --- |
| Qwen3.6-35B-A3B, UD-Q4\_K\_M GGUF (\~22.1 GB) | attention + KV on GPU, experts in DDR5 | main `local` worker | 2 slots × 64k ctx (1 slot when under 10 GB VRAM is free) |
| gpt-oss-20b, MXFP4 GGUF (\~12.1 GB) | fully on GPU | `local-fast` for XS tasks | 1 slot × 32k ctx, loaded only when no `local` task is running |
| LFM2.5-1.2B-Instruct, Q4\_K\_M (\~0.7 GB) | on GPU | local decider (`lfm-tower`) | loaded on demand |
| Qwen3.8-27B, Q3 (\~12–13 GB) | on GPU, short ctx | eval candidate only | not resident |

Both workers cannot be resident at once in 16 GB. `vigild` owns model lifecycle: it keeps Qwen resident, and asks for gpt-oss-20b only when the queue holds XS tasks and no `local` task is running.
- **The open question is the mechanism.** vigild now runs on the Mac and nothing extra may be installed on the tower, so the design's swap script and timer have no control channel.
- **Proposal P6** in `docs/architecture.md` is llama-server's router mode (`--models-dir`, `--models-max 1`, `--sleep-idle-seconds 1800`), which loads on demand and sleeps when idle.
- **Without P6**, vigild swaps over SSH with a forced-command key.
- **Sharing with Hearth.** Before any load, `vigild` reads free VRAM from the tower exporter, and runs one slot when under 10 GB is free. You have the whole tower for now, but the design is ready for sharing it with Hearth.

Swap cost is 10–20 s of load, cheaper than the quality loss of running XS tasks on nothing.

Starting command (tune `--n-cpu-moe` upward until `nvidia-smi` shows \~15 GB used; prefix caching stays on by default and is what makes agent turns fast):

```bash
llama-server -m /models/Qwen3.6-35B-A3B-Q4_K_M.gguf \
  --n-gpu-layers 999 --n-cpu-moe 28 \
  -c 131072 --parallel 2 --jinja \
  --host 100.x.y.z --port 8080 --api-key "$LLAMA_KEY" \
  --metrics --slots
```

Flag notes:
- `-c 131072 --parallel 2` gives two 64k slots.
- `--jinja` and `--slots` are on by default in current llama.cpp, so the flags are harmless but redundant.
- `--metrics` exposes server-wide Prometheus counters. That gives queue depth and throughput, but **not** GPU time per task: the counters have no per-request labels. Per-session GPU attribution comes from the tower exporter (next paragraph).
- Bind to the Tailscale IP, not `0.0.0.0`, and allow only the Mac in the Tailscale ACL.

Run it as a systemd unit on the host (no Docker on the tower). The prebuilt CUDA 12.8 and 13.4 release binaries already include Blackwell (sm\_120). Pin the llama.cpp release tag and the GGUF file hashes in `models.lock`. Worker containers on the Mac reach it through their egress proxy over Tailscale.

**Tower exporter.** A second small systemd service on the tower, the only other thing installed there besides Tailscale and the NVIDIA driver:
- It reads NVML (`nvidia-ml-py`) and `psutil`, pinned.
- Each second it samples GPU utilization, VRAM, CPU, RAM and watts, plus the cumulative energy counter.
- It serves the samples as Prometheus `/metrics` and as JSON.

`vigild` pulls the samples and attributes each second to the sessions with a request in flight on llama-server. It summarizes per session as joules per token and GPU-seconds per token (`docs/architecture.md` B11, B12).

Expect 1–2 concurrent sessions and measure tokens/s on your machine in the first week; the eval section uses those numbers. If the eval shows Qwen3.6-35B-A3B is not good enough for the `local` task shapes, RunPod H100 overflow runs the same OpenCode container against a vLLM endpoint, nothing else changes.

## OpenCode executor

One container per task runs `opencode serve` with the task's clone as its only project; `vigild` drives it over HTTP and reads the event stream. OpenCode v2 shipped on 2026-09-11 with a new config format and a new server API, so the build pins v2 (`v2.0.26`) and uses the v2 permissions array and v2 endpoints ([v2 permissions](https://opencode.ai/v2/docs/permissions/), [v2 server API](https://dev.opencode.ai/v2/docs/api)).

Session flow, per task:

1. `vigild` makes a per-task clone on the host from its local mirror, not a `git worktree`. A worktree's git dir lives outside the mount, so git would fail in the container. vigild writes the task contract to `.vigil/task.json` in the clone and starts the container with the clone mounted at `/work`.
2. The container starts `OPENCODE_PASSWORD=… opencode serve --hostname 0.0.0.0 --port 4096`, and `vigild` waits on `GET /api/info`.
3. `vigild` opens `GET /api/event` (SSE) and keeps it open for the life of the session; every bus event is forwarded to the OTel collector as a span or log under the task's trace id. The v2 stream is documented as lossy: a slow consumer drops it, and events sent while disconnected are lost. So `vigild` reconnects and reconciles from session state.
4. `POST /api/session` then `POST /api/session/{id}/prompt` with the spec rendered as the prompt. The container's `AGENTS.md` is a symlink to the repo's `CLAUDE.md`, so both tiers read the same rules.
5. When the session goes idle, `vigild` calls `GET /api/session/{id}/diff`, checks `files_changed` against `files_in_scope`, runs nothing itself, and tells the worker to commit and push (a `shell` allow rule) and open a draft PR with the worker token.
6. `POST /api/session/{id}/interrupt` on timeout, turn cap, or `vigil kill`. The container is removed; the clone stays until the PR closes.

`opencode.json` baked into the image, in the v2 shape (last matching rule wins, so denies go last). v2 `read` and `edit` resources are relative to the project, so they say `.github/*`, not `/work/.github/*`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": { "tower": { "package": "@opencode/ai/providers/openai-compatible",
    "settings": { "baseURL": "http://tower:8080/v1", "apiKey": "{file:/run/secrets/model_key}" },
    "models": { "qwen3.6-35b-a3b": { "limit": { "context": 65536 } } } } },
  "model": "tower/qwen3.6-35b-a3b",
  "permissions": [
    { "action": "*", "resource": "*", "effect": "allow" },
    { "action": "external_directory", "resource": "*", "effect": "deny" },
    { "action": "edit", "resource": ".github/*", "effect": "deny" },
    { "action": "edit", "resource": ".opencode/*", "effect": "deny" },
    { "action": "edit", "resource": ".claude/*", "effect": "deny" },
    { "action": "read", "resource": "*.env", "effect": "deny" },
    { "action": "read", "resource": "*.env.*", "effect": "deny" },
    { "action": "read", "resource": "*Config.xcconfig", "effect": "deny" },
    { "action": "shell", "resource": "git push*main*", "effect": "deny" },
    { "action": "shell", "resource": "gh pr merge*", "effect": "deny" },
    { "action": "shell", "resource": "gh pr review*--approve*", "effect": "deny" },
    { "action": "shell", "resource": "gh api*", "effect": "deny" },
    { "action": "shell", "resource": "gh secret*", "effect": "deny" },
    { "action": "shell", "resource": "fly *", "effect": "deny" },
    { "action": "shell", "resource": "curl*", "effect": "deny" },
    { "action": "shell", "resource": "*--no-verify*", "effect": "deny" },
    { "action": "webfetch", "resource": "*", "effect": "deny" },
    { "action": "websearch", "resource": "*", "effect": "deny" }
  ]
}
```

v2's `serve` has no `--auto` flag. In serve mode an `ask` becomes a permission request on the API, so the config has no `ask` effects at all. `vigild` answers any permission request it still receives with `reject` (`POST /api/session/{id}/permission/{requestID}/reply`) and logs it. The model key comes from a read-only file (`{file:…}`, checked against v2 in phase 4), never the image or the environment. OpenCode's own docs say the shell resource is raw text and best-effort, so this list is the first layer only; the container (next section) is the real one.

Why OpenCode over OpenHands, in one paragraph: OpenHands bundles its own sandbox and a heavier runtime per session, which duplicates what Docker Compose already gives you, and its agent loop is harder to drive turn-by-turn from outside. OpenCode is a thin server with an OpenAPI spec, a per-session SSE bus, fork/abort/diff endpoints and a permission model that mirrors your guard plugin. For a fleet where the orchestrator owns isolation, the thin server wins. If a future model needs OpenHands-specific tooling, it is one more executor adapter.

## Claude tier and how you spawn sessions

**Claude executor.** A container running the Claude Agent SDK (Python) with these settings:
- **Permissions:** `permission_mode="bypassPermissions"` inside the sandbox. It refuses to run as root, so the container user is non-root.
- **Guard plugin:** ParkAgent's guard, now the `guard@claude-mods` plugin, installed at build time from claude-mods at a pinned commit SHA. claude-mods is private, so the build reads it with a contents:read installation token of the vigil-worker App, passed as a BuildKit secret.
- **Deny list:** the repo's deny list goes in the image's managed settings.
- **Model access:** through vigild's model gateway (P1, owner 2026-10-22). The container gets `ANTHROPIC_BASE_URL` pointing at the gateway and a per-session gateway token as the auth token, on its stdin; it never holds a provider key. The gateway injects the key, meters every call and refuses a call that would cross a budget.
- **GitHub:** none. Workers commit; vigild checks scope and pushes from the host (P2).
- **OTel:** on.

The SDK runs the Claude Code CLI as a child process and exports spans, metrics and log events over OTLP when `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and the three `OTEL_*_EXPORTER=otlp` variables are set ([Agent SDK observability](https://code.claude.com/docs/en/agent-sdk/observability)). `vigild` sets `OTEL_RESOURCE_ATTRIBUTES=vigil.task_id=…,vigil.tier=claude,vigil.repo=…` and `TRACEPARENT` so the run nests under the task's trace. It bills to the API key of the "vigil" org. That org's monthly credit ($100–200) is a hard ceiling, so vigild's caps sit well under it: $5 a day, $2 a task, and reviews count toward the day.

**Reviewer.** Same container image, different prompt: it gets the diff from `gh pr diff`, the issue text, and the checklist (wrong charge or consent bypass, test fails before fix, docs updated, no weakened tests, what could be deleted). It can read the repo but has `edit: deny`. It posts a PR review as comments and returns `findings[]` to `vigild`. Empty findings plus green CI marks the PR "ready for human merge"; anything else re-queues a follow-up task.

**Self-maintenance.** After phase 2, Vigil may run tasks on its own repo, under the same rules and WIP limit, with the vigil-worker App installed on vigil.

**Four ways to spawn, one API.** `vigild` exposes a REST API on the Tailscale interface. The CLI, the MCP server and the web UI are clients of it, so you can dispatch from wherever you are. The web UI (phase 3) is specified in `docs/ui.md`.

| From | How | Example |
| --- | --- | --- |
| Terminal | `vigil` CLI (Python, `uv tool install`, on the Mac mini) | `vigil run parkagent#142 --tier auto`, `vigil batch parkagent-batch.yaml`, `vigil ls --live`, `vigil trace pa-0142`, `vigil kill pa-0142` |
| Claude Code session (your Mac) | `vigil` MCP server added to `.mcp.json`; tools `vigil_run`, `vigil_status`, `vigil_trace`, `vigil_kill`, plus the bridge's `vigil_talk`, `vigil_listen`, `vigil_jobs`, `vigil_approve` (`vigil_approve` files your decision into the inbox; it is not a second decision surface) | "Dispatch the three test tasks from the batch file to the tower and tell me when PRs are open" |
| Browser | web UI served by `vigild`, Tailscale only (`docs/ui.md`) | `/new`: repo, base, backend preset or custom endpoint, spec, limits |
| GitHub | label `vigil:ready` on an issue; `vigild` polls or receives a webhook | coord labels issues; the fleet picks them up |

The Claude Code route is how coord keeps its role: coord decides and dispatches, `vigild` executes and reports. Coord never runs workers itself anymore, which also frees your Max-plan session from doing the cheap work.

`vigil ls --live` streams a table: task, tier, model, status, turns, minutes, cost or GPU-seconds, PR, CI, review. `vigil trace <id>` prints the Phoenix URL and a 30-line summary of the tool calls.

## Security: three layers, no secrets in workers

No session can merge, push to main, deploy, or read a secret. Each of these is stopped in three independent places, so a bypass of one still fails.

| Threat | Layer 1: GitHub | Layer 2: container | Layer 3: harness config |
| --- | --- | --- | --- |
| Merge or approve | vigild's installation token per publish, scoped to the one repo, `contents:write`, `pull_requests:write`, no `administration`, no `workflows`; ruleset on `main` requires PR + checks and blocks force push | **no token in the container** (P2, owner 2026-10-22): vigild pushes and opens the PR from the host | `gh pr merge*`, `gh pr review*--approve*`, `gh api*` denied |
| Push to main | ruleset: direct pushes to `main` blocked | pre-push hook in the image refuses `main`; `.git/hooks` and `.git/config` read-only | `git push*main*` denied |
| Deploy / prod | no Fly or prod credential anywhere | egress allowlist, per session: package registries for workers (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`) and vigild's gateway; no model host and no GitHub host (P1, P2); there is no other route out | `fly *` denied |
| Read secrets | — | no `.env`, no `~/.aws`, no SSH keys mounted; root FS read-only; only `/work` and `/tmp` writable | `read *.env*` denied |
| Escape the worktree | — | only the task's clone is mounted; user is non-root; `--cap-drop ALL`, `--pids-limit 512`, memory cap | `external_directory` denied |
| Change its own rules | the token has no `workflows` permission, so GitHub refuses a push that touches `.github/workflows` | `.opencode/`, `.github/`, `.claude/` mounted read-only over the clone | `edit .github/*`, `edit .opencode/*` denied; guard's self-protection |

**Egress.** Workers run under Docker Desktop on the Mac, where host nftables isn't available, so the build picked the sidecar proxy:
- Each session gets its own `internal` Docker network, which has no route out.
- A smokescreen proxy sidecar is the session's only way out, with an allowlist generated for that session.
- DNS may still resolve outside names under Docker Desktop, so the boundary is the missing route plus the proxy, not DNS.
- The `llama-server` port on the tower is reachable only over Tailscale, and the Tailscale ACL allows only the Mac.

**Tokens and keys.**
- `vigild` mints a GitHub App installation token per publish, for one repo listed in `configs/repos.yaml`, with a 1-hour life, and revokes it after the push and the PR. No container ever holds one (P2).
- No provider key reaches any container. A session holds a gateway token that works only on vigild's gateway, only for its repo and backend, and only until it ends (P1).
- `vigild` meters cost itself (Tracking, below), so no key is shared with the UI.
- Secrets live only in `~/.config/vigil/env` (mode 600) on the Mac, plus the App's `.pem` beside it. The tower holds its own llama-server key. The variable list is in `docs/runbook.md` "Credentials".

Tests run in GitHub Actions on the PR branch, never trusted from the worker's shell. A worker that edits a test file in a non-`tests` task fails the spec gate on return.

Anything that needs a secret (migrations against prod, Fly secrets, Stripe keys) is a task type the router refuses; it opens an issue for you instead.

## Tracking: one trace per session

Every session, from either tier, becomes one trace in self-hosted Arize Phoenix. The session id groups it (`session.id`); the task id is on every span. `tier`, `model`, `repo`, `branch`, `pr_url` and `attempt` are filterable metadata, set on every span. An OpenTelemetry Collector sits in front so both tiers write the same way.

| Source | How it reaches the collector | What you get |
| --- | --- | --- |
| Claude executor and reviewer | CLI's built-in OTLP export (traces + metrics + logs), `TRACEPARENT` from `vigild` | one span per turn, per model request (tokens, latency), per tool call; per-call tokens and cost from `claude_code.api_request` events (Metering, below) |
| OpenCode executor | `vigild` subscribes to `/api/event` and translates bus events (message parts, tool start/finish, idle, permission events) into OTLP spans with the same names | one span per turn and per tool call; tokens from the message usage fields; GPU-seconds and joules from the tower exporter, attributed per session |
| `vigild` itself | its own spans: `vigil.route`, `vigil.spawn`, `vigil.review`, `vigil.escalate` | the routing reason and every state change in the same trace |

Why Phoenix over Langfuse (**reversed, owner decision 2026-10-22**): this design first chose Langfuse for its MIT licence and ClickHouse storage. But self-hosted Langfuse v4 is six containers and wants about 16 GiB, and the Mac mini has 24 GiB with a 12 GiB Docker VM that the workers also need. Phoenix is one container, OTel-native, and the owner judged its licence fine for self-hosted use. The collector stays in front of it, so the exporters never change and Langfuse remains an option for a machine with more memory.

Phoenix ingests OTLP over HTTP (`/v1/traces`) and keeps traces in SQLite under its working directory. It runs with auth on; the collector authenticates with Phoenix's admin secret. Phoenix takes traces only, so the collector drops Claude Code's metrics and log events; per-call metering comes from vigild's gateway, not from telemetry. Hook spans would need Claude Code's detailed beta tracing, which reroutes logs, so they are not collected.

Tool content is off by default; the Claude tier turns on `OTEL_LOG_TOOL_DETAILS=1` so commands and file paths show, and `OTEL_LOG_TOOL_CONTENT=1` only for the reviewer, whose inputs are a diff you already own. Prompts are logged for both tiers because you own the repos.

**Web UI.** Phoenix's trace view is the drill-down. The fleet view is `vigild`'s own web UI (phase 3), specified in `docs/ui.md`:
- these nine pages and nothing else: board, job detail, sessions, decider, review ladder, new session, budget, talk, watch (a full-screen ambient display; owner direction, 2026-10-22);
- the Vigil UI canvas is the visual reference for all nine, Talk and Watch included;
- plain HTML, CSS and a little vanilla JS, with no build step;
- served on Tailscale only, from the same API as the CLI and MCP server.

**Metering.** Every task and session records two kinds of usage.
- **API calls, from phase 1, for any provider.** Each call records input, output, cache-read and cache-write tokens and its cost.
  - All hosted calls: vigild's gateway reads the usage on every response it forwards (P1, owner 2026-10-22).
  - OpenCode executor: they come from the v2 event stream.
  - Cost is always tokens × the dated price table in `configs/prices.yaml`. The SDK's own `total_cost_usd` is a client-side estimate, used only as a cross-check.
- **Tower jobs, from phase 5.** GPU utilization, VRAM, CPU, RAM and watts are sampled every second by the tower exporter. Each sample is attributed to the sessions with a request in flight on llama-server, and summarized as joules per token and GPU-seconds per token. Measured energy replaces the old 0.4 kW estimate.

All of it is exposed as a JSON endpoint and a Prometheus `/metrics` (phase 3), so Hearth can read it, and shown on the job detail page.

## Cost model and GPU lifecycle

The tower has no hourly cost, so the question is only whether the local tier's failures cost more in Claude re-runs and reviews than they save. The eval answers that; the model below is what the eval is checked against. Per-task Claude figures are placeholders to replace with your first week's data (figures marked approximate).

| Line | All-Claude | 30% Claude / 70% local |
| --- | --- | --- |
| Tasks per day | 10 | 3 Claude + 7 local |
| Claude worker cost (approx. $3 S/M task, $9 L task) | \~$45 | \~$20 (Claude tasks are the large ones) |
| Claude reviews (approx. $0.80 each, 1.3 rounds avg) | \~$10 | \~$10 |
| Spec-writing calls for local tasks (approx. $0.30) | — | \~$2 |
| Escalations (20% of local tasks go to Claude on attempt 3, approx. $3) | — | \~$4 |
| Electricity (0.4 kW, \~5 GPU-hours, $0.25/kWh) | — | \~$0.50 |
| Total per day | \~$55 | \~$37 |

At a 20% escalation rate the local tier saves about a third. At 50% escalation it saves nothing, and the right move is to narrow rules 6–8 to the task shapes it passes. `vigild` computes this table from real data every day and shows it on the dashboard, so the decision is never a guess.

**Budget scale.** The table above assumed about $40/day. Your caps are $5/day and $2/task, so expect roughly 3–6 Sonnet tasks a day with their reviews. The 100% Opus audit at rung 0 is the largest line until shapes graduate.

**GPU lifecycle on the tower.** `llama-server` keeps a model loaded while the queue has local work and unloads it after 30 idle minutes, dropping idle power to a few watts. The mechanism is router mode if P6 is accepted, and vigild over SSH if not. Load time is 15–30 s from NVMe, so the first task of a batch pays that once. gpt-oss-20b swaps in only for XS tasks, as described above.

**Overflow.** RunPod serverless (scale-to-zero) running vLLM with a bigger model is the only paid GPU option in the design, and it is off until the eval shows a task shape where the tower's models fail and the hosted model passes. A persistent rented H100 at \~$2–3/hour costs more per day than the all-Claude column; it is never worth it for one developer.

## Eval protocol

The eval is a replay of closed ParkAgent issues with the merged PR hidden, scored by the same CI and the same reviewer. It runs before the routing rules go live and again whenever a model or rule changes.

1. Pick 10 closed issues: 4 tests-from-spec, 2 docs, 2 dependency bumps, 2 small UI/copy. Write the spec for each the way the spec gate requires (Claude does this once; the specs are reused for every candidate).
2. Run all 10 as `local` tasks from the same base commit, for each candidate. Workers: Qwen3.8-27B Q3 (dense, on GPU, short context), Qwen3.6-35B-A3B Q4 (MoE, experts in RAM), gpt-oss-20b (on GPU); DeepSeek V4.1 Flash via a hosted API (eval only, ParkAgent is public). Ceilings: Claude Sonnet and Claude Haiku, so you see where each local model lands between them. Reviewers: the same list, scored against Claude Opus as the auditor.
3. Decider: Jev (via Cloudflare or OpenRouter), Haiku with constrained JSON, and a Liquid AI LFM 1–3B on the tower, each answering the launch question set over the 10 specs plus 20 past issues, judged against your own labels. Score agreement, calibration (does 0.85 confidence mean 85% right), latency and cost per decision.
4. Score each run: CI pass on first PR, reviewer findings count, turns, wall time, cost (Claude) or GPU-seconds (tower), and "would a human merge this unchanged" (you, 2 minutes each). For reviewers: findings matched against the auditor, false findings per PR.
5. Decide per task shape, not per model: a shape stays `local` only if the chosen model passes CI first try on ≥ 70% and averages ≤ 1 finding; a reviewer enters rung 1 only if it matches ≥ 80% of the auditor's findings on the eval set; a decider backend is the default only if its agreement with your labels is ≥ 90% at the act threshold.
6. Store the matrix in `evals/2026-10/results.csv` and link it from the routing section of the repo's docs. Re-run quarterly or on any model swap.

The eval also decides the GPU question: if DeepSeek V4.1 Flash passes shapes the tower fails, a RunPod serverless adapter is worth building; if not, it isn't.

**Learning (planned):** the learning epic's outer loop is a second, weekly replay, with its own reward and its own tune and held-out splits ("Learning loop"). How it relates to this eval and to phase 7 is *Open 3* there.

**Training-data firewall (optional later phase).** Fine-tuning the local model is allowed only on its own attempts graded by your test suite. Every artifact `vigild` stores carries `provenance: claude | local | human`, the training exporter filters on `provenance == local`, and a unit test asserts that a fixture tagged `claude` is rejected. Merged PRs written by Claude are never exported. This keeps you inside Anthropic's terms without relying on memory. **Learning (planned):** the learning epic's tower loop (later, optional, not scheduled) would be the first user of this exporter. Its conflicts with this paragraph are *Open 7* and *Open 8* in "Learning loop".

## Repo layout and build order

A separate repo, `vigil`, with the same rules as ParkAgent (tests first, docs in the same PR, one way per thing, pinned versions ≥ 2 weeks old).

```text
vigil/
  vigild/            FastAPI app: api/, contract/, intake/, decider/, router/, scheduler/, executors/{claude,opencode}/,
                     review/, metering/, telemetry/, github/, web/ (static HTML, CSS, JS; docs/ui.md)
  cli/               `vigil` CLI and the MCP server (same client library)
  scripts/           repo checks run in CI (check-feature-map.py)
  schemas/           task, result and event JSON schemas
  images/            Dockerfiles (linux/arm64): claude-executor (also reviewer/auditor), opencode-executor, egress-proxy
  tower/             llama-server systemd unit, models.lock, exporter/ (NVML + psutil service), check.sh
  compose.yaml       otel-collector, phoenix; vigild itself runs natively under launchd
  configs/           vigild.yaml (fleet coord backend), backends.yaml, routing.yaml, budgets.yaml, prices.yaml, repos.yaml, review.yaml,
                     egress.yaml, opencode.json, managed-settings.json, otel-collector.yaml
  evals/             specs/, run.py, results/
  docs/              design.md, architecture.md, feature-map.md, runbook.md, sources.md, ui.md, decisions/ (ADRs)
  tests/
```

Build order: everything is built and proven with Claude models first, through the Claude executor, so the fleet works end to end before any local model is involved. The tower is the last thing plugged in. This is possible because the only model-specific code is an executor adapter and a backend object; `vigild`, the contract, routing, review ladder, budgets and tracing never know which model is behind a tier.

Build sessions use two models:
- **Opus** for coord, the phase 1 and 2 workers, and every review.
- **Sonnet** for later phases, unless a review sends work back twice.

The per-phase detail (deliverables, credentials, spend) is in `docs/architecture.md` "Seven-phase plan". **Seven phases (owner decision 2026-10-22)**, plus the learning epic after phase 5 ("Learning loop"; an epic, not a phase):

| Phase | Delivers | Done when |
| --- | --- | --- |
| 1. Core | `vigild`, task contract, spec gate, intake scoring, jobs as trees with the state machine and the inbox, the coord loop, backends and presets, the model gateway, budgets (hard stops), per-call metering, Claude executor image with the guard plugin, per-task clone + container + egress proxy, host-side publish, GitHub App tokens, pinned dev images, OTel → Phoenix, CI (lint, types, tests) | an approved spec on ParkAgent produces a coord that writes `spec.md` and `plan.md`; you approve from the inbox; two workers build in their own containers; both open draft PRs through vigil-worker, each with `worker_report.json`; the job tree is readable from one endpoint |
| 2. Review ladder + fleet + CLI/MCP + session proxy | reviewer + auditor, rung-0 audit of 100%, scheduler, WIP limit, escalation (forking full context), `vigil` CLI + MCP server, label intake, the session proxy (read and join a live session), the bridge's bus, MCP tools and `vigil-bridge`, documentation layers 4, 5, 7, verification level 1 | 3 batch tasks run in parallel, reviewed and audited; `vigil kill` works; dispatched once from a Claude Code session |
| 3. Web UI + metering | the web UI in `docs/ui.md` (with `/talk`); metering as JSON and Prometheus `/metrics` | a session started from `/new` shows live on the board; per-call costs sum to the session total; `/metrics` shows it; a screenshot of each page |
| 4. OpenCode executor | OpenCode v2 executor image on Claude through the gateway; both executors pass the same contract tests | the same docs task passes through both executors with identical results and traces |
| 5. Tower | llama-server (router mode), models.lock, tower exporter, GPU metering, tower presets, LFM decider | `tower/check.sh` passes; one local task opens a PR; tokens/s and J/token recorded and shown |
| Learning epic (after phase 5; planned) | Experiments #9 (hidden-test validity), #10 (reward validity) and #11 (swap gap; needs phase 5), then the epic's features in `docs/feature-map.md` "Learning" | each experiment's decision rule applied and recorded under `docs/learning/`; the rest defined with the owner, after the *Open* notes in "Learning loop" are decided |
| 6. Post-merge + fix jobs + auto-merge | the post-merge watcher (48 h window), fix jobs as children with the coord resumed, auto-revert, auto-merge for shapes at rung 3 | defined when the phase is dispatched |
| 7. Eval | `evals/run.py` over workers, reviewers and deciders; routing.yaml and review.yaml edited from results | results.csv committed; first shapes at rung 1; cost table uses real numbers |

After phase 6: the training-data firewall exporter (the provenance tag exists from phase 1), the RunPod overflow adapter if the eval calls for it, and more repos added via `repos.yaml`. The learning epic's tower loop (later, optional, not scheduled) would use that exporter.

## What you set up by hand, and the answers

By hand, once. The exact steps are in `docs/runbook.md` "Credentials":

- [ ] **Mac mini:**
  - Docker Desktop with a 12 GiB VM (Phoenix, the collector and the sessions fit).
  - Tailscale.
  - `~/.config/vigil/env`, mode 600, with the App `.pem` beside it.
- [ ] **Tower:**
  - NVIDIA driver.
  - Tailscale joined, with an ACL allowing only the Mac to reach llama-server and the exporter.
  - NVMe space for about 40 GB of GGUFs.
  - Nothing else: no Docker.
- [x] **GitHub App "vigil-worker"** (verified 2026-10-22):
  - Permissions: `contents:write`, `pull_requests:write`, `issues:write`, `checks:read`, `metadata:read`, `artifact_metadata:read`, and no `workflows`. vigild down-scopes every token to its use.
  - Installed on all eight of your repos; vigild mints only for those in `configs/repos.yaml`, plus claude-mods (read-only, for the guard plugin).
  - Private key kept on the Mac outside any worker mount.
- [ ] **Branch ruleset on `main` in every repo:** PR required, checks required, no force push. The fleet never gets admin.
- [ ] **Anthropic Console:** org "vigil" with its own API key (exists) and a monthly spend limit.
- [ ] **Phoenix:** `PHOENIX_SECRET`, `PHOENIX_ADMIN_SECRET` and the first admin password, generated by you into the env file (`docs/runbook.md`).
- [ ] **Each repo:** an `AGENTS.md → CLAUDE.md` symlink committed, and `docs/feature-map.md` and `docs/architecture.md` kept current, since they are the local model's whole map of the codebase.

Answers to the questions (2026-10-22):

1. **Caps.** The daily Claude cap is $5 and the per-task cap is $2. Reviews count toward the daily cap. The vigil org's credit ($100–200/month) is the outer limit.
2. **Repos.** None after ParkAgent yet, plus vigil itself after phase 2. Private repos never go to hosted APIs, except Jev over Cloudflare zero-retention, and only where `configs/repos.yaml` lists it for that repo.
3. **Hearth and the GPU.** You have the whole tower for now. The design is ready for sharing: it checks free VRAM before loading, and uses one slot when under 10 GB is free.
4. **Where things run.** vigild and the CLI run on the Mac mini. The tower runs models only.

## Build prompt

Paste this into a fresh Claude Code session in an empty `vigil` repo, with this doc exported as `docs/design.md` beside it.

> **As run (2026-10-22):** the coord kickoff actually used extends the prompts below with the changes summarized at the top of this doc:
> - six phases; where these prompts say "phases 1–3" and "phase 4 touches the tower", read phases 1–4 and phase 5;
> - the boundaries with ParkAgent's coord;
> - the guard plugin in containers;
> - the secrets rules.
>
> The four questions are answered above.

**Vigil coord.** Run the build as its own coordinator session in the `vigil` repo, separate from ParkAgent's coord. It dispatches one worker per phase, reviews each PR with a fresh adversarial subagent, keeps a status issue, and never touches ParkAgent. Kickoff, pasted first in a new Claude Code session with Opus:

```markdown
You are vigil-coord, the coordinator for the vigil repo. Work the same way ParkAgent's coord does: you plan and dispatch, workers build in their own git worktrees and branches, you review every PR with a fresh-context adversarial subagent, I merge. Keep a GitHub status issue titled "vigil-coord status" updated after every step. Never run work in the ParkAgent repo; it has its own coord.

The spec is docs/design.md. Read it fully. Then run the build prompt at the end of it exactly as written: ask me its four questions, write docs/architecture.md and docs/feature-map.md as PR 1, and wait for my OK before dispatching phase 1.

Dispatch one worker per phase, in order, and do not dispatch phase N+1 until I merge phase N. Give each worker the phase row from the design doc, the hard constraints, and the "done when" check, and require the check's output in the PR description. Use Opus for the phase-1 and phase-2 workers and for every review; Sonnet for later phases unless a review sends work back twice.

When phase 2 merges, tell me how to point ParkAgent's coord at vigild (the MCP server config to add to its .mcp.json). Until then ParkAgent keeps running workers as it does today.
```

```markdown
Build vigil from docs/design.md. Read it fully first; it is the spec. Do not re-decide what it decides. Where it says "pick at build time", pick, state why in docs/architecture.md, and move on.

Rules (same as ParkAgent): tests fail before the fix; docs/architecture.md and docs/feature-map.md update in the same PR; no new dependency without a one-line reason in the PR; one way to do each thing; every version pinned to a release at least two weeks old. Search the web for the current docs of the Claude Agent SDK (Python, OTel env vars, permission modes, hooks), OpenCode v2 (server API, v2 permissions array, provider config, Anthropic provider), llama.cpp llama-server flags (--n-cpu-moe, --parallel, --jinja, --metrics), Langfuse self-hosting, TypeSafe Jev (API, Cloudflare and OpenRouter routes, question types), Liquid AI LFM serving in llama.cpp with JSON-schema grammars, and GitHub App installation tokens. Cite what you used in docs/sources.md.

Build order is Claude-first and model-agnostic: phases 1–3 use only Claude models (Opus for architecture and first passes, Sonnet otherwise, Jev through Cloudflare Workers AI or OpenRouter as the decider, Haiku with constrained JSON as its fallback, behind one typed-question interface) and must work end to end before phase 4 touches the tower. vigild, the task contract, routing, the review ladder, budgets and tracing must not know which model is behind a tier; only executor adapters and provider config do. One PR per phase, in order. Each PR ends with the phase's "done when" check actually run and its output pasted into the PR description. Do not start phase N+1 until I merge phase N.

Hard constraints, enforce in code and test them:
- No worker can merge, push to main, deploy, read .env*, or escape its worktree. Three layers as in the design doc. Write a test per threat row that spawns a worker container and asserts the action fails.
- No Claude-generated text can enter a training export: provenance tags on every stored artifact plus a test that a `claude`-tagged fixture is rejected by the exporter.
- Budgets and the per-repo WIP limit are hard stops in vigild; tests prove the daily cap and the WIP limit halt dispatch.
- Intake: no task runs without why/value/risk; self-generated tasks need value ≥ 3 and never touch money/auth/consent paths. Test both.
- Review ladder: rung state is data in configs/review.yaml, promotions and demotions are computed from audit match rates, and money-path shapes cannot be promoted. Test the demotion path.
- The task contract JSON is the only interface between vigild and executors; both executors pass the same contract test suite.

Before writing code, ask me these and nothing else: (1) daily Claude cap and per-task cap, (2) repos after parkagent and which are private, (3) whether Hearth shares the GPU, (4) Mac or tower for the CLI. Then write docs/architecture.md and docs/feature-map.md as the first PR with the plan for all phases, and wait for my OK.
```

Sources used for this design: [OpenCode server API](https://opencode.ai/docs/server/) · [OpenCode v2 permissions](https://v2.opencode.ai/permissions) · [Claude Agent SDK observability](https://code.claude.com/docs/en/agent-sdk/observability) · [llama-server manual](https://manpages.debian.org/unstable/llama.cpp-tools/llama-server.1.en.html) · [Langfuse vs Phoenix](https://langfuse.com/compare/arize-phoenix) · earlier chat: Anthropic training-use policy, Muse Glimmer, Qwen3.6/3.8 benchmarks.
