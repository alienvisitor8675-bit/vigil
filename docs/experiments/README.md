# Experiments

One numbered entry per experiment. Earlier experiments are numbered in `docs/learning/README.md` and the issues; this directory starts at #12.

Each experiment runs:
- only after the bench freeze lifts;
- only through vigild;
- after the owner confirms it;
- on the experiment budget line.

Its results and decision are committed here once it has run.

| # | Experiment | Question | Data | Measure | Status |
|---|---|---|---|---|---|
| 12 | omp's edit benchmark against tower-qwen (ADR-0006 H; omp research C14) | How often does the tower's model fail an edit, and does a harness lift it? | omp's typescript-edit-benchmark, run by us | edit success rate, harness off vs on | planned (after the freeze). omp's published figures are not cited (per research note, unverified). |
| 13 | Retry policy (ADR-0006 H) | Does a retry do better from a full-context fork, or from a flush plus the verifier's failure category? | 30 retries on vigil's own tasks or Smoke, never ParkAgent | hidden-test pass rate, cost per retry | planned (after the freeze). #84 already chose flush below attempt 3; this measures that choice. |
