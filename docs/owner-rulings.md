# Owner rulings

The owner's decisions, one line each, newest first within each section. This
is an index. `CLAUDE.md` and the other docs are the binding text; when a line
here and a doc disagree, the doc wins and this line is stale. A ruling that
was replaced stays, marked `(superseded YYYY-MM-DD)`, with the new one above
it. Coord checks this file before asking the owner, and logs which ruling it
applied.

## Merging and PRs
- **2026-10-28**: Coord marks a PR ready on a clean review, not on green CI; auto-merge waits for CI.
- **2026-10-28**: Rebase a PR onto main only, never onto a branch still under review; a stack the owner set up is retargeted after its base merges.
- **2026-10-28**: The main ruleset requires a PR, squash only, the lint, types and test checks, linear history, no force pushes, and no bypass; "require up to date" is off (strict=false, checked through the API at 10:00) and auto-merge is on (#1).
- **2026-10-28**: Coord marks a PR ready and the owner runs `gh pr merge --squash --auto`; rebase only for a textual conflict (#1).
- **2026-10-28**: Merge tiers: docs-only or tests-only with green CI, the owner enables auto-merge on sight; code, the owner merges after a read; pinned paths, the owner reads the diff in person (#1).
- **2026-10-28**: One feature slice per PR, under about 600 lines changed, or split it before dispatch; the rule applies from the next dispatch, and #48, #49 and #58 merge as they are (#1).
- **2026-10-28**: A flaky required check is fixed deterministically (a fake clock or event, no longer timeout, no rerun plugin), with 20 consecutive local runs in the PR (#66).
- **2026-10-28**: Migrations: of two PRs that each add a step, the second to merge appends last; never insert a step before one production has run (#49).
- **2026-10-24**: The owner merges nothing until the focused review of a gateway, verifier or hidden-test PR is clean; the PR keeps `needs-review` until then (#19).

## Money and usage
- **2026-10-28**: Caps: $25 a day (pacing only), $200 a month (the real budget), $2 a task (#88).
- **2026-10-28** (superseded 2026-10-28 by the line above): The daily cap is $10 for the phase 2 done-when run (#63).
- **2026-10-28**: Coord asks for `/usage` only before a run longer than 2 hours, before an Opus worker, or when the last reading is more than 3 hours old; dispatch pauses at 60% weekly or 70% session, with a hard stop at 75% weekly (#1).
- **2026-10-28**: Unattended runs are allowed within the usage lines, after a /compact and with a short brief; the owner can stop them per run (#1).
- **2026-10-28** (superseded 2026-10-28 by the line above): No unattended runs until a spend guard exists (#1).
- **2026-10-27**: Experiment spend (bench) is a separate $30 monthly line, outside the day cap and inside the month cap, and a hard stop (#8).
- **2026-10-23**: Review and audit caps start at $0.40 and $0.80; the review reserve is held once per job when it enters review, and over-cap reviews complete with the overage charged to the daily cap (#1).
- **2026-10-23**: Fallback decider spend has its own $1/day cap with a breaker, and review and audit spend sits outside the per-task cap (#1).
- **2026-10-23**: The coord session cap is $2 and coord and worker output is capped at 16,384 tokens (#15).
- **2026-10-23**: Author spend for a flagged hidden-test task is capped at $1, outside the $2 task cap (#19).

## Models and routing
- **2026-10-28**: Pin exception: llama.cpp b11739 (9 days old when adopted) is allowed until 2026-11-04, when the tower is rebuilt on a pin at least 14 days old (`docs/sources.md`) (#68).
- **2026-10-28**: Routing rule 3 fires on labels only; `architecture_paths` is removed (#61, #64).
- **2026-10-28**: Per-repo `ladder_pinned_paths` keep money-adjacent PRs at rung 0 with an Opus audit; `money_paths` and rule 2 are unchanged (#59).
- **2026-10-28**: Sonnet for delegated work, Opus for coord's own reviews (#1).
- **2026-10-28**: The tower serves Qwen3.6-35B-A3B for rule-6 workers (track C); the first tower-routed job is `vv-26`, with rung-0 Sonnet review and the Opus audit (#1).
- **2026-10-27** (superseded 2026-10-28 by the track C ruling above): Local models are for measurement only; no real job routes to local until the owner has seen the Smoke result (#11).
- **2026-10-23**: OpenRouter stays pinned to the Jev 1.13 model; a bump happens only by PR after the decider eval set is replayed, and a missing or different reported version counts as drift and falls back (#1).
- **2026-10-23**: The Jev route follows the repo's live GitHub visibility, checked at dispatch and before each OpenRouter call; a failed lookup counts as private (#1).
- **2026-10-23**: Coord uses Opus for rung 0, cross-module work and escalation; batch goes through the gateway (#1).

## Security and secrets
- **2026-10-28**: A session never opens a `.env` file or anything under `~/.config/vigil/`, even when the owner asks; the owner opens it in a Terminal of the owner's own (#1).
- **2026-10-26**: Hidden tests ship disabled by default; a flagged task opens an owner-review draft PR (#22).
- **2026-10-24**: Hidden tests are black-box (never import scope, base runner config, worker code only as a subprocess on a read-only checkout), and config overlaps are refused (#19).
- **2026-10-24**: Assertion failures carry an HMAC tag keyed by the host key, which the owner creates (#19).

## Repos (ParkAgent, visibility)
- **2026-10-28**: vigil stays public for now (rulesets and auto-merge need Pro on a private repo, Actions minutes, runner isolation); revisit after phase 2 with GitHub Pro and an isolated runner VM.
- **2026-10-28**: Coord may submit vigild jobs on `thomasbardhi01/vigil`; only the owner approves a spec out of `spec_pending`, and only the owner submits anything for ParkAgent; "never submit" applies only to unattended runs unless the run's brief says otherwise (#1).
- **2026-10-27**: Sandbox hosts are generic; code never names "the tower" (#43).
- **2026-10-24**: The private-repo hosted-model rule is waived for vigil only (hard-coded in `vigild/repos.py`) (#19).
- **2026-10-23**: ParkAgent stays public until 2a's reruns, the phase 1 run, the 2a merge and the allowlist cleanup (with doctor passing) are done; every repo is designed private-first (#1).

## Process (reviews, workers, reports)
- **2026-10-28**: A routing rule's test submits a request through the API and checks the routed task; it never sets the input the rule reads (rule 6 never fired because `has_existing_tests` was never set while its tests set it by hand; #100).
- **2026-10-28**: After round 1, re-review only the new diff; a bug class found once is fixed everywhere in the PR; after two rounds, bring it to the owner (#1).
- **2026-10-28**: Coord checks `docs/owner-rulings.md` before asking the owner, and logs which ruling it applied (#1).
- **2026-10-28**: After #60 and backup (#23) merge, docs-, tests- and fix-shaped work on vigil is submitted as vigild jobs, not built by coord's subagents (#1).
- **2026-10-28**: Briefs say foreground only and no background jobs after the report; coord stops a worker that repeats or stalls for 20 minutes and finishes the step itself (#1).
- **2026-10-28**: Thresholds are pre-registered: the replay rule is fixed on pa-0003's 4 cases before a run, and a case whose status is not done is left out of recall (#49).
- **2026-10-26**: Review order is Sonnet first, Opus only for focused verifier, gateway and hidden-test reviews, at most 2 rounds, class-level text (#1).
- **2026-10-26**: At the round limit the owner may read the final diff in place of a further review (#28).

## Product decisions (per feature)
- **2026-10-28**: The lean coord packet ships behind `coord_packet` (default `classic`); the flip to `lean` is its own one-line PR carrying the spec-replay numbers for pa-0003 and vi-0002, merged on sight if both pass (#87).
- **2026-10-28**: Backup: the marker is a plain copy, doctor checks freshness at 36 hours, and the check is skipped only while the plist is not installed (#57).
- **2026-10-28**: `~/vigil-backups` is accepted as the interim backup destination until an external drive is attached; #23 closes when the first nightly set passes `vigil restore` (#23).
- **2026-10-28**: vigild runs under launchd so a reboot does not stop the factory; the plist sets `PATH` from a placeholder, and coord does the switch-over with the owner present (#65).
- **2026-10-28**: Kill follow-up is required before the done-when run: terminal states are final, sessions are marked killed, and no page or item opens on an abandoned job (#60).
- **2026-10-27**: Bench smoke results post as one vigild comment per vigil PR, updated in place, and never on ParkAgent or any other repo; the mix is 7 PolyBench TypeScript and 3 SWE-bench Pro tasks (#8).
- **2026-10-26**: The plan gate refuses a reused worker ID, and a job enters `in_review` only if every planned task has an open PR or is superseded (#25).
- **2026-10-23**: Job artifacts go to the vault, not the target repo (#30).

## Coordination (coord's own rules)
- **2026-10-28**: Parallel tracks don't share hot files (`vigild/jobs/runner.py`, `vigild/packets/assemble.py`, `vigild/db.py` `MIGRATIONS`, or the same docs table); if they must, they run in sequence.
- **2026-10-28**: No stacked PRs unless the owner asks.
- **2026-10-28**: Coord's status holds one merge order; after each merge the next PR is rebased onto main and checked.
- **2026-10-28**: Coord assigns each migration-adding PR its step name at dispatch.
- **2026-10-28**: `scripts/check-all.sh` runs on the committed HEAD before every push, by workers and coord.
- **2026-10-28**: The long-term fix for docs table conflicts is #96 (one file per feature, generated tables).

## Not adopted (don't re-propose)
- **2026-10-28** (ADR-0006 I): no multi-agent teams or debate; no memory or recall tools for workers; no LLM-generated instruction files and no longer CLAUDE.md (layer 3 is measured before it grows); no LLM-as-judge selection; no mid-trajectory critics; no RL on weights; no evolved verifiers; no more prompt text. The incident-to-playbook epic stays gated by replay.
- **2026-10-28** (omp / OMH research): neither project as a library or a layer; no inter-agent messaging bus (`ask_coord` stays the only channel: a single writer, read-only subagents); no fall-through model chains (the private-repo rule, budget predictability); no external memory server (one source of truth); no LLM-as-judge in rewards; omp's approval tiers never substitute for the guard plugin or the egress proxy; snapcompact, stream-abort rules, collab links and search providers are out of scope.
