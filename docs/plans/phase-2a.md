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

## 2b (next PR, branches from 2a's merge)

This section is recorded here so the decisions travel with the plan. 2b
gets its own plan file and done-when.

- **Clients:**
  - the `vigil` CLI (`run`, `batch`, `ls --live`, `trace`, `kill`);
  - `vigil mcp` (B8), with `vigil_run`, `vigil_status`, `vigil_trace`,
    `vigil_kill`, `vigil_jobs` and `vigil_approve`.
  - **`vigil_approve`** (owner, 2026-10-23):
    - It acts on inbox items: `approve_spec`, `ready_to_merge`, and
      answering a question.
    - Each call is scoped by item id and logged with `provenance: human`
      and the channel.
    - It is **the only MCP tool that changes job state.** `vigil_talk`
      never does.
- **Session proxy:**
  - read and join a live transcript, with owner messages logged as
    `provenance: human`;
  - the `code` event (the last edit, redacted) that phase 3's `/watch`
    reads.
- **Bridge:** the bus, `vigil_talk` and `vigil_listen` (the text is quoted
  verbatim and logged with the relay model), and `vigil-bridge`.
- **Documentation:**
  - doc layers 4, 5 and 7, the search subagent and the staleness score;
  - **ADR-0002:** plain Git only in vigil-owned clones.
- **Layer 9, the product model:**
  - `docs/product/` per repo, in pdd-kit's shape. Evaluate `npx
    pdd-toolkit` against the no-build-step and pin rules.
  - Specs cite the product entries they change; stage 10 updates them, and
    a stage-4 contradiction check runs.
  - Seed vigil's own model.
  - **ParkAgent's is seeded by the owner, with Opus, as a ParkAgent PR**,
    not generated. 2b leaves a stub and a seeding guide only.
- **Verify level 1:** Hypothesis (including stateful tests for the FSM)
  and Schemathesis. SwiftCheck/swift-testing waits on the iOS image
  question.
- **Per-attempt event sequence** (owner ruling on #14, line 1095): every
  attempt gets its own event sequence, so the PR footer's event-log range
  can point at one attempt. Until it exists, the footer shows `pending`.
- **Context engineering** (owner, 2026-10-23):
  - **Prefix order:** tools, then system, then the fixed packet, then
    volatile content.
    - A cache breakpoint goes after the fixed packet.
    - Cached and uncached input are metered separately, and each session
      records its cache-hit rate.
  - **Research:** a sub-session that returns a summary of at most 2k
    tokens. The coord plans from the summary. Workers start fresh, with
    only the spec, the plan and their scoped files.
  - **Compaction:** at 50% of the context window, the session writes
    `progress.md` (done, next, decisions) and continues from it in a fresh
    context. The event log records each compaction.
  - **Packet budgets per role,** enforced by `packets`:
    - worker: 30k tokens;
    - coord: 60k tokens;
    - reviewer: the diff, the spec and the issue.

    Every packet's token count is logged.
  - **After phase 5:** research sub-sessions route to the local model by
    default.
  - **Baseline: the coord on pa-0002** (ParkAgent #272, 2026-10-23), from
    its metered calls:
    - **Shape:** 2 coord sessions, 14 Opus calls, 1.24M input tokens and
      18.7k output. Session 1 was the first draft, 10 calls. Session 2
      followed a spec-gate failure, 4 calls.
    - **Cost:** $1.48 as run. The CLI already caches with 5-minute writes,
      and every call after the first in each session read the prefix from
      cache. Uncached, the same calls would have cost $5.34.
    - **Where the money went:**
      - Each session's first call wrote a 74–79k-token prefix: $0.40 and
        $0.38. That's 52% of the $1.48.
      - A cache breakpoint shared across the two sessions would have
        turned session 2's write into a read, saving about $0.36 (24%).
      - A 60k coord budget would also cut both prefix writes.
    - An earlier attempt of the same job (11 calls) went unmetered because
      of the migration bug fixed in #16. It isn't in this baseline.
- **Done-when:**
  - one task is dispatched from a Claude Code session through `vigil mcp`;
  - `vigil kill` works;
  - `vigil_approve` approves a spec, and `vigil_talk` can't change state;
  - a joined message is tagged `human`;
  - all of it runs on the test repo and vigil only.
