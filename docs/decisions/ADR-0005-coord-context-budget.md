# ADR-0005: Coord context budget

- **Status:** proposed, 2026-10-28. The owner set the direction, the build order and the targets. This PR is the design; each part is built as its own PR afterwards.
- **Deciders:** the owner. Recorded by vigil-coord.
- **Related:** #78 (a fresh context for replans and fix rounds, PR #84), #79 (the floor refusal's message), the vi-0001 and vi-0002 cap failures of 2026-10-28.

## Context
The coord costs too much, and its context grows until a cap refuses it. This is production's record up to 2026-10-28: 8 jobs, 16 coord sessions, all on Opus.

| Measure | Now | Target |
|---|---|---|
| Coord $ per S job (all its coord sessions) | $2.36 over 2 jobs | ≤ $0.40 |
| Coord share of all metered spend | 65% ($10.51 of $16.25) | ≤ 35% |
| First-draft spec-gate pass rate | 0% (0 of 4) | no drop |
| First-pass acceptance (tests pass, no high or medium finding in round 1) | 67% (4 of 6) | no drop |

**Where the tokens go:**
- `assemble_coord` sends `README.md`, the whole `docs/feature-map.md` and the whole `docs/architecture.md` (109k characters). It drops whichever doesn't fit in 120k characters.
- A fresh coord's first call is 49k to 119k input tokens. All of it is written to the 5-minute cache: the sessions together wrote 1.42M tokens and read 6.35M.
- The only prefix shared across sessions is about 10k tokens: Claude Code's own system prompt and tools.
- A replan and a fix round **fork** the previous session's whole transcript:
  - vi-0001's replan coord forked 93 entries. Its first call was 171k tokens ($0.87 of its $2 reservation), and its second call was refused.
  - vi-0002's fix round forked 146 entries. Its first call was 117k tokens, and the call after it was refused under the $1.20 task cap.

**Every first-draft gate failure was mechanical:**
- a bare-directory scope entry (`server/src/`, `docs/`);
- a worker scope outside spec.md's files in scope.

vigild could have filled in all of these itself.

## Decision
The parts are built in this order. Each is one PR under about 600 lines, measured with the four numbers before and after.

### 1. A lean coord packet (first)
`assemble_coord` sends:
- **Stable block first**, identical across the coord sessions of one repo until those files change, so it is cached:
  1. the role prompt;
  2. `CLAUDE.md` whole (layer 1);
  3. a **feature-map index**: one line per row, with the feature, its status and its files. No descriptions.
- **Then scope-driven excerpts** under per-layer budgets, the same way `assemble_worker` uses `LAYER_BUDGET_TOKENS`:
  - the full feature-map rows whose files meet the job's paths;
  - the `docs/architecture.md` sections that name those paths;
  - the files in scope.

  The job's paths come from the request, the notes and vigild's pre-fill (part 5).
- **Job content last:** the request contract, the notes, gate errors, untrusted text (fenced) and the output schema.
- **Never** the whole feature map or the whole architecture doc. `README.md` goes only if a budget has room.
- **Measure:** `tokens_cache_read` against `tokens_cache_write_5m` and `_1h`, per coord session, before and after.

### 2. Replans and fix rounds start fresh (#78, PR #84)
- **What they start from:** a replan coord and a fix-round worker start from:
  - `spec.md`;
  - `plan.md`;
  - `spec_changelog.md` (when there is one);
  - the owner's note;
  - the failure.

  The failure is the verifier summary, and for a fix round also the review and audit findings and the PR diff. Untrusted parts are fenced.
- **No fork below attempt 3** (`routing.escalate_attempt`). This also covers a retry and a plain replan.
- **The one exception proposed:** re-attaching to a session interrupted by a container crash. The owner rules on it.
- **Test bar:** the first call is under 20k input tokens. The packet test bounds the packet (chars/4); the live figure, which adds Claude Code's own prompt and tools, is read from `model_calls` after the merge.

### 3. The coord's model by shape (`configs/routing.yaml`)
The single `coord: claude-opus` becomes rules, top to bottom, first match wins. The rule name goes on the session like `tier_reason`.

| Rule | Coord |
|---|---|
| attempt ≥ 3 (escalation) | Opus |
| label `architecture` or `migration` | Opus |
| label `tests` or `docs` | Sonnet |
| a fix (a fix job, or label `fix`), size S–M | Sonnet |
| anything else | Opus (as today) |

**Size at coord time.** The coord runs before there is a plan, so its size comes from vigild's pre-fill estimate (part 5): files and lines in scope. Once a plan exists, its own size is used.

### 4. One Explore subagent per job, plus a decisions log
- **The Explore subagent:**
  - The coord may run **one** Haiku Explore subagent per job. It returns a context pack of at most 2k tokens: the paths, symbols and tests the job touches.
  - The subagent decides nothing. Scope, ACs and risks stay with the coord.
  - It is billed to the coord's session through the gateway. The gateway must accept a Haiku model on a coord token, and it caps Haiku output on a coord session at 2048 tokens.
  - This touches the gateway (a pinned path), so the owner reads that diff.
  - **Private repos:** a repo whose policy lists no hosted Anthropic backend gets no Explore subagent. Today only `thomasbardhi01/vigil` lists one.
- **`decisions.jsonl` per job:**
  - **Where it lives:** a vault artifact, never a branch file.
  - **Who writes to it:** the coord and vigild append to it; workers never do.
  - **Each line:** `{ts, by: coord|vigild, kind, text}`, with `text` of at most 300 characters.
  - **Checks:** vigild validates each line against a pydantic model and runs it through `redact`.
  - **Use:** every later session of the job (replan coord, worker, fix round, reviewer) gets it in its packet, fenced as data.

### 5. vigild pre-fills the mechanical parts
Before the coord runs, vigild computes these and writes them into the spec and plan skeleton the coord receives:
- runner files, money paths and pinned paths;
- the scope rules: exact files only, and each worker's scope inside spec.md's files in scope;
- `test_cmd` candidates: the repo's `dev_suites` and the test files that match the scope.

The coord writes the goal, the acceptance criteria and the risks. The spec gate is unchanged: it still checks everything, including what vigild filled in.

### 6. Compaction thresholds in the executor image
CLI 2.1.306 was checked, by reading its binary in `vigil-session/vigil:4abf8a55`.
- **Compaction can be set:**
  - `CLAUDE_CODE_AUTO_COMPACT_WINDOW` sets the window in tokens; the CLI's own warning says to set it to enforce a limit.
  - `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` sets the trigger percentage. Forked sessions ignore it.
  - `DISABLE_AUTO_COMPACT` turns compaction off.

  vigild sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` per role in the session's environment. The starting values are tuned by the spec replay (part 7): coord 60k, worker 100k.
- **Tool-result clearing cannot be set.** The CLI does plan `clear_tool_uses_20250919` edits (the `context-management-2025-06-27` beta). But a server-side feature flag (default off) turns it on, and it only fires after 65 minutes idle. Its thresholds are hard-coded: keep 12 tool uses, plus a minimum to clear.
  - No environment variable or setting reaches it.
  - The alternative is for vigild's gateway to add `context_management` to the requests it forwards. That is a money-path change, and it is **not** part of this ADR unless the owner says so.

### 7. Spec replay on the experiment line
- **The harness:** it re-drafts the approved specs of **pa-0003** and **vi-0002** under each change, on the experiment pool. Every row is `eval-only`, as with the review replay (#47, `replays`).
- **What it reports:** a diff of the new draft against the approved spec, the gate result, the coord's cost and the cache read/write split.
- **The bar:** the gate pass rate, with no drop.
- **pa-0003 is ParkAgent:** each replay of it runs only after the owner confirms that run, as for any ParkAgent run.

## Measuring
- The four numbers in the Context table go in every morning report.
- They are computed read-only from the production DB by the coord's script: per job, all coord sessions, bench jobs excluded.
- Their definitions are the table's. Pre-change rows are the baseline: compare by job `created_at` against each part's merge date.

## Consequences
- **Cost:** a coord drafting an S job should cost a fraction of today's $0.46–$0.93 per session. The cap failures of 2026-10-28 cannot recur below attempt 3.
- **Less context for the coord:** it no longer sees the whole architecture doc. What it misses, it reads with tools or gets from the Explore pack. The spec replay is the check that drafts don't get worse.
- **Cheaper reruns:** a replan or fix round loses the earlier session's reasoning. The spec, the plan, the changelog and `decisions.jsonl` carry what must survive.
- **Two pinned-path diffs for the owner:** the gateway in part 4 and the router in part 3.

## Alternatives considered
- **Raise `coord_session_usd`.** That treats the symptom; the context still grows with every replan.
- **Only drop the architecture doc from the packet.** This was already happening when it didn't fit, and the coord still paid for the feature map and for forks.
- **Fork, then compact.** Compaction itself is a model call over the whole transcript, and forked sessions ignore the percentage override.

## Open questions for the owner
1. **Crash re-attach:** may a crash re-attach keep resuming the interrupted session below attempt 3, as the one exception to "no fork"?
2. **Coord model default:** should the coord default stay Opus for jobs that match no rule (features, L fixes)?
3. **Replay timing:** should the spec-replay harness (part 7) land right after part 1, so that parts 2–6 are each measured as they merge? The build order otherwise stays as given.
4. **Tool-result clearing:** should vigild's gateway add tool-result clearing itself? That is a separate money-path ADR.
