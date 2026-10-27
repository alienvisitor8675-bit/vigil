# Phase 1 run report: ParkAgent #272

The phase 1 done-when, run end to end on a real repo through vigild only:
1. An approved spec on ParkAgent produced a coord, which wrote `spec.md` and `plan.md`.
2. The owner approved it from the inbox.
3. Workers built in their own containers.
4. Each worker opened a draft PR through the vigil-worker App, with `worker_report.json`.

**Task:** ParkAgent #272 (`vigil:active`) asked for two things: a feature-map line checker that catches `path:line` references pointing at a doc comment or at the wrong declaration, and the stale references in `docs/feature-map.md` fixed. The job was `pa-0002`; `pa-0001` was lost to finding 0, below.

## Timeline and cost
| When (owner's clock) | What |
|---|---|
| Fri 2026-10-23 18:15 | Job submitted (`pa-0001`). The coord's first call was refused by the session cap (see finding 0). |
| Fri 2026-10-23 | `pa-0002` submitted after #15. Its coord ran unmetered (#16). The owner approved the spec. |
| Fri 2026-10-23 | **w1 (doc fixes) PR merged:** ParkAgent #316. w2 (the checker) failed acceptance: findings #1 and #2. |
| Fri–Mon | Hotfixes #16, #17, #18, #19, #21, #22 and #25. ADR-0003 (#24) moved hidden-test hardening to its own track. |
| Mon 2026-10-26 | Replan with an owner note, which hit finding #4 (fixed by #25). Replanned again, and w3 built. |
| Tue 2026-10-27 | **w3 (the checker) PR merged:** ParkAgent #319, after the owner reviewed it by hand. Hidden tests were disabled. pa-0002 moved to `merged` on vigild's next PR poll. |

**Cost:**
- **Metered:** $3.71 for `pa-0002`, across its coord, worker and verifier attempts.
- **Unmetered:** another 11 Opus coord calls on the first `pa-0002` attempt, before #16. The DB missed a migration, so `record_call` failed after each upstream call succeeded. The Anthropic Console shows the real figure.

**Honest line:** most of the elapsed time went into the findings, not the task. That's what phase 1 was for.

## Findings
| # | Finding | Type | Fix | Status |
|---|---|---|---|---|
| 0 | An Opus coord's first-call worst case ($3.08 with an uncapped `max_tokens`) exceeded the $1 coord reservation. | verification (budget) | #15: output cap, coord reservation of $2, sessions queue when the day is full | fixed |
| 0b | The live DB's schema version was set by another branch, so main skipped a migration, and every call's metering failed silently. | coordination | #16: every idempotent migration runs on start; a metering failure fails closed | fixed |
| 1 | **Unstable test IDs.** A suite put a generated key in its test titles, so test IDs differed between runs and the executed-tests ⊇ base check could never match. A secondary leak: unredacted PEM key blocks in an inbox body. | verification | #17: two base runs, unstable IDs matched by count; `redact` covers PEM; a scrub migration | fixed |
| 2 | **Runner-file tasks.** A task whose scope was a pinned runner file was verified against base's copy of that file, so the task's own change was never tested. | verification | #18, #19, #21 and #22: flagged tasks, hidden tests (now **disabled by default**), and an owner-review PR | fixed; hidden tests off until ADR-0003 |
| 3 | **`depends_on` not honoured.** The checker task depended on the doc task but started from base in parallel. | coordination | #28: 2a's graph scheduler starts a task only once every task it depends on has published (B43) | fixed |
| 4 | **Reused task ID and a silent `in_review`.** A replan renumbered its task to an existing merged ID. Nothing was dispatched, and the job went to `in_review` with no page. | coordination | #25: the plan gate refuses reused IDs; a job enters `in_review` only if every planned task has an open PR; reconcile pages stuck jobs | fixed |
| 5 | **A test's network cleanup removed the shared OTel collector.** | coordination | #26: tests remove only resources that carry their own per-run label | open |

**Side findings:**
- **Hidden-run isolation gaps:** several classes of isolation gap between the subject and the test process were found in review and partly fixed, including a shared writable cache path and a service reachable across uids. The remainder, and the separate-subject-container design, are in **ADR-0003 (#24)**. `hidden_tests.enabled` stays off until that series merges with a clean review.
- **Stale risks in an amended spec:** the coord amended the note's items but kept three risks that had since been answered: broker support, the count question, and the previous failure's cause, which was finding #2, not the interface. Tracked as **2b backlog item 2.12**.
- **An inactive author contract:** the hidden-test author contract in the amended spec sits inactive while `hidden_tests.enabled` is off.
- **Job artifacts in ParkAgent:** #316 and #319 both carried `.vigil/pa-0002/` into ParkAgent, and the owner removed them by hand. **Fixed by #30:** job artifacts stay in the vault, and published branches never carry `.vigil/`.

## What worked
- **False passes:** the verifier refused three. It refused w2 twice (findings #1 and #2) rather than accept a result it couldn't trust, and it held the w1 retry until unstable IDs were handled.
- **Budget caps:** the gateway kept every metered call inside its caps. The one breach of the cap ledger (finding 0b) was a recording failure, now fixed to fail closed.
- **Replan with a note:** it amended the approved spec instead of redrafting it. The owner checked a diff of the spec and a changelog, not a new document.
- **Isolation:** each worker built in its own container, and every PR went out through the vigil-worker App, with no token in a worker.

## Process changes adopted during the run
- Verifier, gateway and hidden-test PRs carry `needs-review` until a fresh-context review is clean.
- Reviews run on Sonnet first. Opus is used only for focused verifier, gateway or hidden-test reviews, with at most two rounds.
- Security issues are described as classes, never as exploit steps.
- Development runs use a scratch `VIGIL_DATA_DIR` and never `~/vigil-data`.
