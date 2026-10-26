# Phase 2a plan: the pipeline

**Phase 2 is split into two PRs** (owner, 2026-10-23):
- **2a, the pipeline,** is this PR.
- **2b, the clients and knowledge,** branches from 2a's merge.

Each has its own done-when. The worker is Opus (architecture.md,
"Seven-phase plan").

**Gate.** The **phase 1 done-when run on ParkAgent** must complete against
`main` before this PR is opened (owner, 2026-10-23). The work happens on the
branch until then. The owner names the ParkAgent issue once ParkAgent's
fleet has drained its PRs.

Every item below names what it delivers and the tests that must fail
first. Paths are the expected ones. The worker may move them, and the docs
move with them.

## 0. Carry-overs from phase 1 (found after #7 merged)

- **Credential helper.** vigild's docker CLI calls fail with
  `docker-credential-desktop: executable file not found` unless Docker
  Desktop's `bin` is on `PATH`.
  - Fix: `DockerImages`, and compose where vigild calls it, resolve the
    CLI's directory and pass a `PATH` that includes it.
  - Test: a build under a `PATH` without it.
- **Pin rule.** Every Dockerfile starts with an unpinned
  `# syntax=docker/dockerfile:1`.
  - Fix: pin the frontend by digest, to a release on or before 2026-10-08.
  - Test: a check that fails any unpinned `syntax=` line.
- **Exit noise.** `vigil dev-image` prints `ValueError: I/O operation on
  closed file` at exit, because the docker client is never closed. Close it.
- **Verifier marker readable by the test.** PREPARE runs as root with only
  SETUID/SETGID, and drops to uid 10001 for the prepare hook and the test,
  so the test can no longer read the marker (architecture.md B33).
  ParkAgent's hook starts PostgreSQL, so the hook runs as 10001 too.
- **Runbook: secrets the owner generates** (owner, 2026-10-23).
  - The owner creates self-generated secrets, never an agent:
    `printf 'NAME=%s\n' "$(openssl rand -hex 32)" >> ~/.config/vigil/env`.
  - To rotate one: delete the line, run the command again, restart vigild.
  - Rewrite the runbook's credentials table to match. Phoenix's two
    secrets need their prefix only if Phoenix still demands a letter and a
    digit; check that, and say so.

## 1. Review ladder (stages 5 and 6)

- **Delivers:**
  - **Reviewer session:** Sonnet, fresh context, read-only tools. Writes
    `review.json`.
  - **Auditor session:** Opus. Writes `audit.json`, listing the findings the
    reviewer missed and a match rate.
  - **Rungs, in `configs/review.yaml`:** each shape has a rung and starts at
    rung 0, with a 100% audit.
    - Promotion and demotion come from match rates, weighted by verify
      level, per design.md.
    - **`money`, `auth` and `consent` shapes stay at rung 0 permanently.**
    - **`deps` is capped at rung 1** (owner, 2026-10-23).
  - **Fix rounds:** a finding of severity medium or higher goes back to the
    same worker as a fix round. Two rounds with findings trigger routing
    rule 5 (escalation).
- **Tests:**
  - an empty `review.json` fails the stage gate;
  - a missed high finding demotes the shape;
  - money, auth and consent can't leave rung 0, and deps can't pass
    rung 1;
  - a fix round reuses the worker's remaining budget.

## 2. Scheduler and fleet

- **Delivers:**
  - **Scheduler:** a dependency graph, three sessions in parallel
    (`configs/vigild.yaml`). The verifier stays one at a time.
  - **WIP limit:** four open fleet PRs per repo. At the limit, dispatch
    pauses and the inbox shows the stack.
  - **Intake:** the `vigil:ready` label, polled. A webhook comes later.
  - **Escalation forks full context:** an escalated attempt starts from the
    failed attempt's whole transcript (SDK session fork), not a summary.
- **Tests:**
  - a graph with a diamond dependency runs in order;
  - a fifth PR waits;
  - a labelled issue becomes a `spec_pending` job exactly once;
  - an escalated attempt's first packet carries the prior transcript.

## 3. Resume (design: "checkpoint and resume")

- **Delivers:**
  - Every session id is stored, along with each worker's last bundled
    commit.
  - **Crashed or killed worker:** it resumes from its last commit, in a
    fresh container on the same session image. Its transcript is resumed,
    it inherits its budget remainder, and its turn count carries on.
  - **vigild restart:** reconcile **re-queues** work that's safe to resume
    (image key unchanged, budget left, under the attempt cap). Everything
    else blocks, as in phase 1.
  - **Deps:** a parent task auto-resumes when its deps job merges (B39).
  - **Re-plan:** the coord resumes with the failure in its packet. Phase 6
    reuses this for fix jobs.
- **Tests:**
  - a worker killed mid-task resumes from its last commit and spends no
    more than its remainder;
  - after a vigild restart with one safe and one unsafe job, the safe one
    is re-queued and the unsafe one blocked.

## 4. Circuit breakers

- **Delivers:** one module (`vigild/breakers.py`), one table and one inbox
  page type. A tripped breaker pauses dispatch for its scope and pages
  once. The owner resets it from the inbox.

  | Breaker | Scope | Trips when |
  |---|---|---|
  | daily / monthly cap | global | the gateway's hard stops (existing; the breaker unifies the paging) |
  | per-task cap | task | the reservation is exhausted (existing) |
  | WIP | repo | four open fleet PRs |
  | escalations per day | global | over `max_escalations_per_day` (config) |
  | fix-job depth | job tree | over 3 (enforced in phase 6; the breaker exists now) |
  | backend errors | backend | N consecutive 5xx/timeouts; the route falls to the next preset, with a half-open probe after a cool-down |
  | repo failures | repo | K consecutive failed or abandoned jobs |
  | verifier unknowns | repo | M `unknown` verdicts in a row (a broken image or runner, not the worker) |

- **Tests:** one test per breaker that trips it, checks that dispatch for
  its scope pauses and exactly one page opens, and then resets it.

## 5. Deps jobs (B39)

- **Delivers:**
  - **Executor:** deterministic, no model. A lockfile-only update runs in a
    build container that has registry egress, no secrets and no gateway
    token.
  - **The 14-day rule** applies to every package the lockfile pulls in.
  - **Rebuild and verify:** the image is rebuilt, and the full suite runs
    offline.
  - **Review:** each deps job opens its own PR at rung 1, and the owner
    always reviews it.
  - **Spawn and resume:** a child job is spawned from a `needs_deps` block,
    and the parent auto-resumes when it merges (item 3).
- **Tests:**
  - a too-new transitive dependency fails the job, with the list;
  - a manifest edit outside the dependency fields fails;
  - the parent resumes on the new image key.

## 6. Worker Stop hook

- **Delivers:**
  - A `Stop` hook in the executor layer's managed settings.
  - A Claude worker can't end its session while `worker_report.json` is
    missing or invalid, or while its own last `test_cmd` run is red.
  - The hook runs the spec's `test_cmd` in-session with a timeout, and
    doesn't trust a marker file.
  - It allows the stop after the task's turn limit, or after three blocked
    stops, and records `stop_hook_exhausted` on the result.
  - The verifier stays the only source of truth.
- **OpenCode workers** get the same check through the SSE idle handler in
  phase 4, together with the OpenCode executor.
- **Tests:**
  - a worker that tries to stop on a red test is sent back, with the hook's
    message in its transcript;
  - the cap releases it;
  - a missing report blocks the stop.

## 7. Carry-overs from the phase 1 run and main's hotfixes (#15–#19)

Fold these in when 2a rebases onto main. `depends_on`, the pause at
checkpoint, the queued page and the vault rule moved to the 2b backlog's
Tier 1 (1.5–1.8).
- **The budget queue:**
  - a waiter re-checks the job's state and stops if the job was
    abandoned;
  - the queue is first-in, first-out;
  - tests cover abandon and restart while a session is waiting.
- **The peak test** includes the review-stage reserve.
- **Runner-file tasks** (finding #2): permanently rung 0, never counted
  toward ladder graduation, with a test.
- **Migrations:** 2a's steps append after main's; nothing is renumbered.
  2a's development runs never use `~/vigil-data`; they set
  `VIGIL_DATA_DIR`.

## Done when (2a)

All of this runs on **the test repo and vigil only** (owner, 2026-10-23).
Nothing writes to ParkAgent.

1. Three batch tasks run in parallel through the scheduler, and are
   reviewed and audited at rung 0.
2. One breaker trips, pages, pauses dispatch, and is reset from the inbox.
3. A killed worker resumes from its last commit and finishes.
4. A Stop-hook block shows up in a transcript.
5. A deps job opens its PR on the test repo, and the parent resumes after
   it merges.
6. `vigil doctor` passes, apart from the optional Cloudflare entries.

**Spend.** If the done-when would cross one day's $5, it runs over two
days and is reported per day.

## 2b backlog (one list, consolidated by the owner on 2026-10-23)

This replaces the 2b notes added during 2026-10-23. Nothing new is added
here: items are only deduplicated and tiered.

Every item has one **owning module**, and either a **tier** or a
**trigger**. 2b gets its own plan file and done-when. This list is its
source.

**Standing decisions:**
- **No change to the verifier's stance.** Vigil keeps fresh-checkout
  verification, with no verdict cache and no flaky-test quarantine.
  Unstable test IDs are handled by #17's double base run.
- **Unverified industry figures** from outside reviews are not used.
- **One host key** for every HMAC: `VIGIL_HOST_KEY`, domain-separated
  (`"hidden-tag:"`, `"fence:"`, and so on). The owner creates it; no
  session ever does.

**pa-0002 token audit** (the baseline): $1.97 over 36 metered calls. The
coord was 75% of that ($1.48 for 14 Opus calls over 2 sessions). Writing
the prompt prefix to cache was 45% (79–98k tokens per coord session, 39–43k
per worker), and no session reused another's prefix. Two causes: the fence
nonce is random on every assembly, and 5-minute cache writes expire between
attempts. `cache_control` passes through the gateway unchanged. Packet
section sizes aren't logged. The 11 calls of the first coord attempt went
unmetered, before #16.

### Tier 1: next

| # | Item | Owning module |
|---|---|---|
| 1.1 | **Kill switch.** A host-side flag outside any repo and outside `configs/`. `vigil stop` sets it and `vigil resume` clears it. The gateway checks it on every call; vigild checks it before every dispatch, push and PR. Test: a session can neither set nor clear it. This PR also lists it in #14's host-policy section in design.md as "host state, never repo-editable, never session-reachable" (ruling 7). | `vigild/killswitch.py` |
| 1.2 | **Exactly-once push and PR on resume.** Push, PR open and PR comments carry idempotency keys derived from job, task and attempt. Status is written before the action, and the result is re-read from GitHub afterwards. A resume never re-fires an irreversible action without re-checking its approval. Rehearsal test: crash mid-commit, resume, and assert the action happened exactly once. | `vigild/github/publish.py` |
| 1.3 | **Coord model by change type** (ruling 1). Opus for rung-0 shapes, cross-module design and escalations; Sonnet for docs, tests and refactors. The rule goes in `configs/routing.yaml`, and this PR updates design.md's routing text, which until now said the coord is always Opus. | `vigild/router/` |
| 1.4 | **Stable prefix with the HMAC nonce.** Order: tools, then system, then the fixed packet, then volatile content, with a cache breakpoint after the fixed packet. Within the fixed packet: repo-wide, then job-wide, then task-specific, byte-identical across sessions of one model. The fence nonce is `HMAC(VIGIL_HOST_KEY, "fence:" + job_id)`, and the content-must-not-contain-nonce check stays. Test: an issue body can't predict the nonce. A coord redraft continues its warm session. Cached and uncached input are metered separately, with the cache-hit rate per session; a caching check runs in CI. | `vigild/packets/fence.py` |
| 1.5 | **`.vigil/<job>/` kept in the vault.** It stays on the job branch while the job runs. Before the PR is marked ready, vigild commits its removal and links the vault copy with a `vigil:` reference. #14's playbook folder is unaffected. Test: a ready PR's diff has no `.vigil/<job>/` path. | `vigild/github/publish.py` |
| 1.6 | **`depends_on`** (phase 1 run finding #3). A dependent task is dispatched after the task it depends on, from that task's head. | `vigild/scheduler/` |
| 1.7 | **Pause at checkpoint.** A mid-run budget refusal pauses the session at its last checkpoint, with raise-cap, resume and abandon actions. | `vigild/jobs/runner.py` |
| 1.8 | **Queued page.** A session queued for more than 2 hours pages the owner. | `vigild/jobs/runner.py` |
| 1.9 | **Re-audit** (trigger: tiers 1.3 and 1.4 landed). Run the token audit on the next ParkAgent job and compare against $1.97. Repeat after 2.1. | `vigild/kpis.py` |

### Tier 2: 2b core

| # | Item | Owning module |
|---|---|---|
| 2.1 | **Packets v2**, one epic, covering: the repo map (paths plus one-line summaries in place of full documents; this is the packet map); per-role budgets enforced, worker 30k, coord 60k, reviewer = diff + spec + issue, with design.md's per-layer budgets nested inside each role's total; when a packet is over, the lowest-priority layer is trimmed first, and which one is logged (ruling 4); every section's size is logged; per-file git context; lessons by glob (`.vigil/lessons/` in place of layer 5, per #14); rule-based tool-output trimming (truncate, deduplicate, keep head, tail and failure lines, capped, before any model summary, with tool-result sizes recorded first); compaction at 50% of the context window into `progress.md`, logged in the event log; packets citing invariant IDs (2.7). | `vigild/packets/` |
| 2.2 | **#14 Stage A metrics and failure detectors.** The metrics are the list in #14. The detectors are pattern-based, with no LLM: `scope_creep`, `success_hallucination`, `tool_misuse`, `cascading_errors`, `context_loss`. Daily tag distribution, with a page above 5%. Every finding is tagged spec, coordination or verification; phase 1's run findings are #1 verification, #2 verification and #3 coordination. | `vigild/kpis.py` |
| 2.3 | **Evidence footer with "what changed and why".** #14's Stage A footer, written by vigild. It opens with a **template, not a model** (ruling 5): spec intent, files changed with line counts, and the test delta. The per-attempt event sequence goes in the footer's event-log range; until it exists, the range shows `pending`. | `vigild/github/publish.py` |
| 2.4 | **Merge-risk grade.** #14 Stage A. Advisory; it only ever raises review. | `vigild/review/risk.py` |
| 2.5 | **Canary tokens.** Fake AWS, `.env` and GitHub credentials in every session container. Reading or exfiltrating one pages the owner and stops the session. The egress proxy and the gateway's body scan both match on them. Test it. | `vigild/executors/canary.py` |
| 2.6 | **The original 2b scope**, unchanged: the CLI (`run`, `batch`, `ls --live`, `trace`, `kill`); `vigil mcp` (B8) with `vigil_approve` as the only state-changing tool; the session proxy (read, join, the `code` event); the bridge (`vigil_talk`, `vigil_listen`, `vigil-bridge`); doc layers 4, 5 and 7, with the search subagent and the staleness score; ADR-0002; verify level 1 (Hypothesis, Schemathesis). | `cli/vigil/`, `vigild/mcp/`, `vigild/proxy/`, `vigild/bridge/` (one per sub-item) |
| 2.7 | **Layer 9 with invariant IDs.** `docs/product/` per repo, with invariant IDs plus their owning files and tests. Packets cite them by ID, and the hidden-test author receives the invariants relevant to its task. vigil's own model is seeded; ParkAgent's is seeded by the owner. | `vigild/product.py` |
| 2.8 | **Per-role output and thinking budgets**, with terse output templates. *Untiered by the owner; placed here.* | `vigild/gateway/service.py` |
| 2.9 | **Lean retries.** A retry starts from the spec, the plan and the failure categories, not the full history. *Untiered; placed here.* | `vigild/jobs/runner.py` |
| 2.10 | **Spend-rate alert.** Page when hourly spend exceeds 3× the trailing median, separately from the caps. *Untiered; placed here.* | `vigild/breakers.py` |
| 2.11 | **Docs-freshness check.** After a merge, flag any doc layer that cites changed code, and file it as `vigil:proposed`. This merges with 2.6's staleness score: one check. *Untiered; placed here.* | `vigild/docs/staleness.py` |
| 2.12 | **Replan changelog lists the resolved risks it removed.** When a replan amends a spec, the changelog also names every risk it dropped because it has since been answered. Phase 1 run: the coord kept three stale risks (owner, 2026-10-26). *Untiered by the owner; placed here.* | `vigild/jobs/runner.py` (amend path) |

### Tier 3: trigger-gated

| # | Item | Trigger | Owning module |
|---|---|---|---|
| 3.1 | **Fault drill**: provider 429 and 500, the tower offline, vigild killed mid-job, disk full; each with its expected outcome. | before the first overnight run | `tests/drills/` |
| 3.2 | **Benchmarks, scorers, harness ablations** (model fixed, one harness feature toggled, run locally) and the **Batch API** (seeded-defect runs, benchmark validation, scorers, nightly audits). The Batch API goes **through the gateway** (ruling 3): a batch path with a worst-case reservation at submit and settlement on result. | #14 Stage B | `vigild/bench/` |
| 3.3 | **Owner audit plus explain-back, one inbox item.** A random 10% of passed PRs asks the owner to audit the PR and explain the change. The results are **labelled data only**, for scorer calibration and reviewer-prompt evals, and are **never ladder evidence**; the ladder stays on the seeded-defect SPRT (ruling 6). **Review-independence data:** every review records its worker/reviewer model pair, with a different family preferred. It is recorded, not enforced, until phase 5 (ruling 2). | 30 merged jobs | `vigild/review/audit.py` |
| 3.4 | **Improvement proposals and evolution invariants.** Before writing Stage C's rules, read Bernstein's `invariants.py` and `circuit.py` (Apache-2.0) for ideas only; adapted text goes in `docs/sources.md` with the licence. | #14 Stage C | `vigild/proposals/` |
| 3.5 | **Research sub-sessions.** A summary of at most 2k tokens for the coord, routed to the local model. | phase 5 | `vigild/research.py` |

### Dropped for now
- **Hash-chained events** (`events.prev_hash`).
- **FTS5.**
- **Structured docs beyond invariant IDs:** the machine-readable feature-map rows.

### Owner rulings on the overlaps with design.md (2026-10-23)
1. **Coord model:** Opus for rung-0 shapes, cross-module design and escalations; Sonnet for docs, tests and refactors. design.md and `routing.yaml` are updated in 1.3's PR.
2. **Review independence:** recorded, not enforced, until phase 5.
3. **Batch API:** goes through the gateway, with a worst-case reservation at submit and settlement on result. It's in Tier 3, at Stage B.
4. **Packet budgets:** per-layer budgets nested inside each role's total. When a packet is over, the lowest-priority layer is trimmed first, and which one is logged.
5. **"What changed and why":** a template made of spec intent, files changed with line counts, and the test delta. No model.
6. **Owner audit:** labelled data only, for scorer calibration and reviewer-prompt evals. It is never ladder evidence; the ladder stays on the seeded-defect SPRT.
7. **Kill switch:** host state outside `configs/`, listed in #14's host-policy section as "host state, never repo-editable, never session-reachable".
8. **2a's §7:** fine as recorded.

### 2b done-when (carried over unchanged)
- One task is dispatched from a Claude Code session through `vigil mcp`.
- `vigil kill` works.
- `vigil_approve` approves a spec, and `vigil_talk` can't change state.
- A joined message is tagged `human`.
- All of it runs on the test repo and vigil only.
