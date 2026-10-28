# ADR-0006: Harness thinning and verifier hardening

- **Status:** proposed, 2026-10-28. The owner set the items, the order and what is not adopted. This PR is the design; each part is built as its own PR.
- **Deciders:** the owner. Recorded by vigil-coord.
- **Related:** ADR-0005 (coord context budget); #84 (fresh replans and fix rounds); the omp / OMH research items of 2026-10-28; `docs/design.md`'s opt-in mutation testing (§ "Mutation testing" and the learning loop's item 16), which part A2 supersedes.
- **Sources:** the research notes behind this ADR are the owner's. No citation from them enters `docs/sources.md` until it has been fetched and checked. Until then a claim from them is marked "per research note, unverified".

## Context
Two pressures meet here:
- **A thinner harness for strong models.** Claude workers need bash and few tools, while the tower's model may need more help.
- **A harder verifier for weak signals.** A green test run only means something when the tests can catch a wrong patch, and when nothing in the checkout leaks the answer.

Both must be measured, not assumed. Every number below comes from vigild's own verifier, audits and metering.

## Decision
Build order:
1. this ADR;
2. C, D and E (small);
3. A1;
4. A2 and F (phase 2b);
5. B, with the phase 4 executor decision.

G is docs only and can be done any time. H waits for the bench freeze to lift.

### A. Verifier hardening (phase 2b, first)
1. **No history in the verifier's checkout.**
   - The verifier's checkouts carry no `.git` history: a bare tree, or a shallow export with a new `.git`. Nothing reachable in them goes past the commit under test: no objects, no alternates, no refs.
   - Today the checkout already gets "a new `.git`" (`vigild/verifier.py`). A1 confirms that no history object is reachable, and adds the test.
   - **For bench tasks this is mandatory:** the gold patch must never be reachable from the checkout's history.
   - **Test:** in each of the after, before and hidden runs, `git log`, `git cat-file --batch-all-objects`, `git rev-list --all` and the alternates file show nothing beyond the one tree. For a bench task, the gold patch's blob ids are absent.
2. **Gold-gated mutation strengthening of acceptance tests.**
   - A local model on the tower generates mutants of the gold patch (bench) or the merged patch (vigil's own tasks).
   - A test is kept as a strengthening test only if it kills at least one mutant and still passes on gold.
   - The verifier report records mutants tried, killed and surviving per task.
   - **Report-only first:** no gate reads it until the owner says so.
   - It runs in its own slot with its own timeout, never in the 15-minute verify path.
   - It **supersedes** the design's opt-in mutation testing (mutmut, Stryker, cargo-mutants), which merges into this item.

### B. Harness by model class (phase 4 contract, with the executor decision)
- **One axis per preset class** in `configs/routing.yaml`, read by the executor:

  | Class | Gets |
  |---|---|
  | `tower` workers | a planning scaffold, typed edit tools, stall intervention, tool-result shortening |
  | `claude` workers | bash first, minimal tools, no scaffold |

- **Data, not code:** the class is a field of the preset, never `if "claude" in model`.
- **Measured, not assumed:** pass rate and cost per class on the same tasks, before either harness becomes a default.
- **Scope:** this lands with the phase 4 executor contract (omp research item C8). The contract stays: NDJSON, exit codes, `prompt_result` vs `session_settled`, the gateway as the only key holder.

### C. Reviewer findings carry evidence and confidence
- **Evidence:** each finding in `review.json` carries `evidence: test | diff_line | spec_criterion | runtime_log`, plus that evidence's locator: a test id, a file:line, an AC id, or a log excerpt id. A finding without evidence is refused at parse, and the reviewer is told why.
- **Confidence:** `confidence` (0–1), as already relayed (omp research item A6).
- **Packaging:** one PR, one schema change, `schemas/*.json` regenerated. Phase 7's auditor match rate gains confidence as a second dimension.

### D. Deny-and-continue
- **The worker continues:** when the guard plugin refuses a tool call, the worker is told the call was refused and to find another route, and it carries on.
- **Counting:** vigild counts denials per session from the transcript events.
- **Breaker:** a breaker trips at **3 consecutive** or **20 total** denials in one session. It is a row in the existing `breakers` table, it pages the job, and the session stops.
- **The guard stays the guard:** nothing here relaxes a rule. It only stops a session that keeps pushing on one.

### E. Trust-ladder inputs
- **What the ladder may read:** `configs/review.yaml` states that no ladder statistic reads a worker report, a transcript, or model reasoning. Only audits and verifier outcomes count.
- **Enforced by a test:** a test checks the ladder's inputs, meaning the tables and artifacts `vigild/review/ladder.py` reads, against that allowlist.

### F. The session as the durable thing (phase 2b)
- **Recovery:** vigild is recoverable from the `events` table and the vault alone.
- **Docker test:** kill vigild mid-job, then restart it. The job resumes from its last checkpoint with **no duplicate spend**: no second reservation, and no call metered twice.
- **Executors stay adapters** behind the JSON contract.

### G. Phase 7 design edits (docs only, now)
These are written into `docs/learning/README.md`:
- **Held-out hidden tests rotate,** and a test that gated a change is never reused.
- **A frozen-baseline arm** in every comparison.
- **A variance floor** from at least 3 base runs. A difference inside it is no difference.
- **A resource floor and a 3× ceiling** per task.
- **A parallel-sampling baseline at a matched budget** before any evolved template is credited.
- **A leakage critic** over template diffs.
- **A cumulative regression budget** per release.

### H. Experiments (after the bench freeze lifts), numbered in `docs/experiments/`
- **#12:** omp's edit benchmark against tower-qwen (omp research item C14).
- **#13:** retry policy. A full-context fork against a flush plus verifier-category feedback, over 30 retries, scored by hidden-test pass rate. It runs on vigil's own tasks or Smoke, never ParkAgent. #84 already chose flush below attempt 3; this measures that choice.

### I. Not adopted (owner rulings; don't re-propose)
- No multi-agent teams or debate.
- No memory or recall tools for workers.
- No LLM-generated instruction files, and no longer CLAUDE.md. Layer 3 is measured before it grows.
- No LLM-as-judge selection.
- No mid-trajectory critics.
- No RL on weights.
- No evolved verifiers.
- No more prompt text.
- The incident-to-playbook epic stays gated by replay.

## Consequences
- **What can be trusted:** a green verifier run means more. Its checkout can't leak the answer (A1), and its tests are shown to catch wrong patches (A2, report-only first).
- **Where harness complexity goes:** only where a measured class needs it (B). Claude workers get less, not more.
- **Review findings become checkable** (C). A reviewer can't assert without pointing at something.
- **A denied tool call is a signal, not a dead end** (D). A session that keeps hitting the guard stops.
- **The ladder can't be gamed by what a worker says about itself** (E).
- **A vigild crash costs time, not money** (F).

## Alternatives considered
- **Trust the worker's own test report:** already ruled out ("test results come only from the verifier"). A2 goes further: even the verifier's pass needs tests that can fail.
- **One harness for every model:** simpler, but unmeasured. B measures before it splits.
- **An LLM judge in place of evidence fields:** not adopted (I).
