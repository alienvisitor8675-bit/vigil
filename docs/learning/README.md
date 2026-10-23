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
