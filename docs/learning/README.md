# Learning

This is the index for Vigil's learning epic. It is design only for now.
- **The spec:** `docs/design.md` "Learning loop" (owner spec and rulings, 2026-10-23). It covers:
  - the principle;
  - the loops;
  - the reward (`gate.json` from the verifier, `reward.json` assembled and signed by vigild);
  - hidden tests and measurement;
  - what Vigil borrows and what it keeps out;
  - the owner's 22 decisions.

  Its stage changes are marked **Learning (planned)** in the same doc's job execution protocol. No open questions remain.
- **The plan:**
  - `docs/feature-map.md` "Learning": the epic's rows, phase **L**, after phase 5;
  - phase 7, "Eval", which holds the outer loop (weekly replay);
  - `docs/architecture.md` "Learning epic" and "Phase 7".
- **The first three experiments,** in order. Each runs only after the first ParkAgent run, only through vigild, after the owner confirms, and on its own budget line:
  1. [#9](https://github.com/thomasbardhi01/vigil/issues/9): hidden-test validity, measured unfiltered;
  2. [#10](https://github.com/thomasbardhi01/vigil/issues/10): reward validity;
  3. [#11](https://github.com/thomasbardhi01/vigil/issues/11): swap gap, which blocks on phases 4 and 5.
- **Related:** [#8](https://github.com/thomasbardhi01/vigil/issues/8), "vigil bench", covers the SWE-bench Pro public sample. #8 draws more than 50 tasks: tasks 1–50 in seed order are the anchor, reported and not tested, and tuning uses tasks 51 onward. The owner confirmed this on 2026-10-23.

Each experiment's results and decision are committed here when it has run.

## Phase 7 design rules (ADR-0006 G, owner 2026-10-28)
- **Held-out hidden tests rotate;** a test that gated a change is never reused.
- **A frozen-baseline arm** in every comparison.
- **A variance floor** from at least 3 base runs; a difference inside it is no difference.
- **A resource floor and a 3x ceiling** per task.
- **A matched-budget parallel-sampling baseline** before any evolved template is credited.
- **A leakage critic** over template diffs.
- **A cumulative regression budget** per release.
- **Paired-benchmark prompt tuning** (omp research C12): a per-model calibration paragraph stays only while a paired run (per instance, bootstrap CI95 plus McNemar) shows it helps; a drift test fails if a tuned family vanishes from the doc. OMH's own finding was token savings, not pass rate (per research note, unverified).
- Experiments from #12 on are numbered in `docs/experiments/`.
