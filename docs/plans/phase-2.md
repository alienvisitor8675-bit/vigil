# Phase 2 plan: review ladder, fleet, clients, session proxy

Status: **plan only.** This file is the first commit of phase 2's PR. No code
work is dispatched, and the PR isn't opened, until the **phase 1 done-when
run on ParkAgent** has completed against `main` (owner, 2026-10-23). The owner
names the ParkAgent issue once ParkAgent's fleet has drained its PRs.

The worker is Opus (architecture.md, "Seven-phase plan"). Each item below
names what it delivers, the tests that must fail first, and what it adds to
the done-when. Paths are the expected ones; the worker may move them, and
the docs move with them.

## 0. Gate and carry-overs from phase 1

- **Gate:** the ParkAgent done-when output is pasted into #7's thread and the
  status issue before this PR leaves draft.
- **Carry-overs** (found after #7 merged, 2026-10-23):
  - **Credential helper.** vigild's docker CLI calls fail with
    `docker-credential-desktop: executable file not found` unless Docker
    Desktop's `bin` is on `PATH`. `DockerImages` must resolve the CLI's
    directory and pass a `PATH` that includes it. Test: a build under a
    `PATH` without it.
  - **Pin rule.** Every Dockerfile starts with an unpinned
    `# syntax=docker/dockerfile:1`. Pin the frontend by digest (a release
    on or before 2026-10-08). A check fails any unpinned `syntax=` line.
  - **Exit noise.** `vigil dev-image` prints `ValueError: I/O operation on
    closed file` at exit (the docker client isn't closed). Close it.
  - **Optional hardening, cheap.** The verifier's PREPARE runs as root with
    only SETUID/SETGID, and drops to uid 10001 for the prepare hook and the
    test. The marker is then no longer readable by the test
    (architecture.md B33). ParkAgent's hook starts PostgreSQL, so the hook
    must run as 10001 too.

## 1. Review ladder (stages 5 and 6)

- **Delivers:**
  - a reviewer session (Sonnet, fresh context, read-only tools) that writes
    `review.json`;
  - an auditor session (Opus) that writes `audit.json` with the findings
    the reviewer missed and a match rate;
  - `configs/review.yaml`: rung per shape, starting at rung 0 with a 100%
    audit. Promotion and demotion come from match rates weighted by verify
    level, per design.md. `money` and `deps` shapes are never promoted
    past rung 1.
  - Findings at or above medium go back to the same worker as a fix round.
    Two rounds with findings trigger routing rule 5 (escalation).
- **Tests:** an empty `review.json` fails the stage gate; a missed high
  finding demotes the shape; the money shape can't be promoted; a fix
  round reuses the worker's budget remainder.

## 2. Scheduler and fleet

- **Delivers:**
  - a dependency-graph scheduler: three sessions in parallel
    (`configs/vigild.yaml`), while the verifier stays one at a time;
  - a WIP limit of four open fleet PRs per repo. At the limit, dispatch
    pauses and the inbox shows the stack;
  - `vigil:ready` label intake, polled (a webhook comes later);
  - escalation that **forks full context**: the escalated attempt starts
    from the failed attempt's whole transcript (SDK session fork), not a
    summary.
- **Tests:** a graph with a diamond dependency runs in order; a fifth PR
  waits; a labelled issue becomes a `spec_pending` job once; an escalated
  attempt's first packet includes the prior transcript id.

## 3. Resume (design: "checkpoint and resume")

- **Delivers:**
  - Every session id is stored, along with each worker's last bundled
    commit.
  - A crashed or killed worker resumes from its last commit, in a fresh
    container on the same session image, with its transcript resumed, its
    budget remainder inherited, and its turn count continued.
  - At restart, vigild's reconcile **re-queues** work that is safe to
    resume (image key unchanged, budget left, under the attempt cap)
    instead of blocking it. The rest blocks, as in phase 1.
  - Auto-resume of a parent task when its deps job merges (B39).
  - Coord resume for re-plan, with the failure in its packet. Phase 6
    reuses this for fix jobs.
- **Tests:**
  - kill a worker mid-task; it resumes from its last commit and spends no
    more than its remainder;
  - restart vigild with one safe and one unsafe job: one is re-queued,
    the other blocked.

## 4. Circuit breakers

- **Delivers:** one module (`vigild/breakers.py`), one table, and one inbox
  page type. A tripped breaker pauses dispatch for its scope and pages
  once; the owner resets it from the inbox.

  | Breaker | Scope | Trips when |
  |---|---|---|
  | daily / monthly cap | global | the gateway's hard stops (existing; the breaker unifies the paging) |
  | per-task cap | task | the reservation is exhausted (existing) |
  | WIP | repo | four open fleet PRs |
  | escalations per day | global | over `max_escalations_per_day` (config) |
  | fix-job depth | job tree | over 3 (enforced in phase 6; the breaker exists now) |
  | backend errors | backend | N consecutive 5xx/timeouts. The route falls to the next preset, with a half-open probe after a cool-down |
  | repo failures | repo | K consecutive failed or abandoned jobs |
  | verifier unknowns | repo | more than M `unknown` verdicts in a row (a broken image or runner, not the worker) |

- **Tests:** one test per breaker that trips it, checks that dispatch for
  the scope pauses and a single page is opened, and resets it.

## 5. Deps jobs (B39)

- **Delivers:**
  - the deterministic deps executor: no model, a lockfile-only update in a
    build container with registry egress, no secrets and no gateway token;
  - the 14-day rule applied to every package the lockfile pulls in;
  - an image rebuild and the full suite offline;
  - its own PR at rung 1, with owner review;
  - the child-job spawn from a `needs_deps` block, and auto-resume on merge
    (item 3).
- **Tests:**
  - a too-new transitive dependency fails the job with the list;
  - a manifest edit outside dependency fields fails;
  - the parent resumes on the new image key.

## 6. Worker Stop hook

It didn't land in phase 1.

- **Delivers:**
  - A `Stop` hook in the executor layer's managed settings. A Claude worker
    can't end its session while:
    - `worker_report.json` is missing or invalid, or
    - its own last `test_cmd` run is red.
  - The hook runs the spec's `test_cmd` in-session with a timeout. It
    doesn't trust a marker file.
  - It allows the stop after the task's turn limit, or after three blocked
    stops, and records `stop_hook_exhausted` on the result.
  - The verifier stays the only source of truth.
- **OpenCode workers** get the same check through the SSE idle handler, in
  phase 4, with the OpenCode executor. No OpenCode executor exists before
  then.
- **Tests:**
  - a worker that tries to stop with a red test is sent back, with the hook
    failure in its transcript;
  - the cap releases it;
  - a missing report blocks.

## 7. Clients: CLI and MCP

- **Delivers:**
  - the `vigil` CLI: `run`, `batch`, `ls --live`, `trace` and `kill`;
  - `vigil mcp` on the MCP Python SDK v2 (B8), with the tools `vigil_run`,
    `vigil_status`, `vigil_trace`, `vigil_kill`, `vigil_jobs`, and
    `vigil_approve`, which acts on inbox items. All are bearer-token
    clients of the REST API.
- **Tests:**
  - every MCP tool against a stand-in API;
  - `vigil kill` stops a live session and settles its reservation.

## 8. Session proxy and bridge

- **Delivers:**
  - **Session proxy:** read and join a live session's transcript. Owner
    messages are logged as `provenance: human`. It also emits the `code`
    event (the last edit, redacted by `vigild.redact.redact`) that phase 3's
    `/watch` reads. Fix the event's shape now.
  - **Bridge:** the message bus, the MCP tools `vigil_talk` and
    `vigil_listen` (the relayed text is quoted verbatim and logged with the
    relay model), and `vigil-bridge`.
- **Tests:**
  - a joined message reaches the session tagged `human`;
  - a planted secret never reaches a `code` event;
  - the relay can't alter the quoted text.

## 9. Documentation and knowledge

- **Delivers:**
  - **Layer 4:** ADRs, with a PR-template check for `architecture` changes.
  - **Layer 5:** `docs/knowledge/lessons.jsonl`, one line per closed job,
    distilled from `close.md`.
  - **Layer 7:** a derived code map, cached per commit.
  - the read-only search subagent;
  - the staleness score.
  - **Layer 9: the product model** (owner addition, 2026-10-22).
    - `docs/product/` per repo holds entities, screens, capabilities and
      rules, in pdd-kit's product-model shape.
    - Evaluate `npx pdd-toolkit` for map and verify. Adopt it only if it
      fits the no-build-step rule and the pin rules (14 days; it would be
      the first npm tool in vigil). Otherwise write the equivalent in
      Python.
    - Stage 1 specs must cite the product entries they change, stage 10
      updates them, and a stage-4 check flags code that contradicts the
      model.
    - Seed vigil's model, and ParkAgent's from its `docs/feature-map.md`.
      Seeding ParkAgent's is a write to ParkAgent; it waits for the owner's
      go-ahead.
- **ADR-0002:** vigil-owned clones (mirrors, vault, task clones) use plain
  Git only. jj and the like are fine on developer machines.
- **Tests:**
  - a spec that changes a product entity without citing it fails the gate;
  - stale lessons are pruned at 90 days;
  - the code-map cache rebuilds when the commit changes.

## 10. Verification level 1

- **Delivers:**
  - Hypothesis property tests, and Hypothesis stateful tests for vigild's
    FSM;
  - Schemathesis against vigild's own OpenAPI;
  - `verify: 1` criteria in the spec gate.
- **SwiftCheck/swift-testing** need macOS. They wait on the iOS image
  question, like the `visual` verify level (phase 4).

## Done when (proposed; extends architecture.md's)

1. Three batch tasks run in parallel and are reviewed and audited (rung 0).
2. `vigil kill` works.
3. One task is dispatched from a Claude Code session through `vigil mcp`.
4. One breaker trips, pages, pauses dispatch, and is reset from the inbox.
5. A killed worker resumes from its last commit and finishes.
6. A Stop-hook block is observed in a transcript.
7. A deps job opens its PR, and the parent resumes after its merge.

All on the fixture repo, plus vigil itself (self-maintenance becomes
allowed after this phase). ParkAgent is used only if the owner says so.

**Spend:** architecture.md estimated about $4.65 for the original scope, at
worst. The additions (Stop hook, resume, breakers, deps job) add live runs.
If the done-when would cross one day's $5, it runs over two days and is
reported per day.

## Open questions for the owner

1. **Split phase 2 into two PRs?** Recommended.
   - **2a, the pipeline:** carry-overs, the review ladder, scheduler,
     resume, breakers, deps jobs and the Stop hook.
   - **2b, the clients and knowledge:** CLI/MCP, session proxy, bridge,
     documentation layers 4/5/7/9, and verify level 1.

   One PR for all ten items would be about twice the size of #7, which took
   five review rounds. The phase rule would read "one PR per sub-phase".
2. **Does `vigil_approve` act on inbox items?** That is the coord's reading
   so far; it was never confirmed.
3. **Where does the done-when run?** The fixture repo and vigil, or
   ParkAgent too?
