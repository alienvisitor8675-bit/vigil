# ADR-0007: Local-model improvement loop

- **Status:** proposed, 2026-10-28. This is the design only. The build starts when the bench freeze lifts, after the phase 2 done-when run.
- **Deciders:** the owner. Recorded by vigil-coord.
- **Related:**
  - #8 (vigil bench);
  - #45 (bench slice 1a: every row of a bench job is `eval-only`), which this ADR **amends** for the train split;
  - ADR-0004 (sandbox hosts);
  - ADR-0006 (the verifier's no-history checkout, gold-gated mutation, experiments #12 and #13);
  - CLAUDE.md "Provenance".

## Context
The tower's local model is the cheap worker. Making it better takes three things:
- **Its own successful trajectories** as training data.
- **A fair test** of whether training helped.
- **A training-data firewall** that the loop can't leak through.

Today every bench row is `eval-only` (#45), and the exporter takes `local` only. Nothing from the bench can train anything yet. That's safe, but it's a dead end for the local model.

## Decision

### 1. The bench split
Every bench task belongs to exactly one split, fixed when the task is loaded and recorded on the task:

| Split | Run by the local model | Run by a hosted model |
|---|---|---|
| **train** | `local`; the exporter may take it, if part 3 allows | `eval-only` |
| **eval** (including held-out Fresh, part 4) | `eval-only` | `eval-only` |

- **What it amends:** this replaces #45's "every row of a bench job is `eval-only`" for one case only: a **local-model** run of a **train-split** task. Everything else stays `eval-only`.
- **Hosted runs never train anything.** That is the design's rule that any hosted provider's artifacts carry `eval-only`.
- **Where the split comes from:** the task loader assigns it from the dataset's own split, or from a seeded split recorded in `docs/sources.md`. It never comes from anything the run produces.
- **When the docs change:** CLAUDE.md's "Provenance" line and `Store.row_provenance` change in the build PR, not here.

### 2. Dedup by repo and issue
- **One record per repo and issue:** a train or eval task is identified by its `(repo, issue)`; for a dataset without issues, by the upstream PR or commit.
- **No crossing splits:** the same `(repo, issue)` may never appear in both splits, or twice in the train split. The loader refuses a duplicate and names it.
- **No near-copies:** a task whose gold patch matches an eval task's patch, by normalised diff hash, is dropped from train.

### 3. Verified-pass trajectory export
A trajectory is exported only when all of these hold:
- the task is in the **train** split;
- the model is **local**;
- the **verifier** (never the worker's report) passed it, with:
  - hidden tests green;
  - must-fail-first held;
  - no weakened tests;
  - the ADR-0006 A1 checkout, with no history.

**One JSONL line per trajectory:**

| Field | Contents |
|---|---|
| `task` | `repo`, `issue`, `split: train`, `base_sha` |
| `harness` | the harness config hash: the executor image digest and the preset |
| `model` | the model id, its digest and its quant |
| `messages` | the transcript as the gateway saw it, with tool calls and results, after `redact` |
| `patch` | the final diff |
| `verifier` | the outcome, the JUnit summary and the verify level |
| `provenance` | always `local` |
| `source_digest` | a sha256 over the task, the patch and the verifier record |

**Never exported:**
- secrets, after `redact` and a second scan;
- raw container logs;
- anything from an eval-split task;
- a trajectory whose `source_digest` no longer matches.

The exporter refuses the whole batch if any line breaks these rules. It never silently drops lines.

### 4. A pre-registered bar on held-out Fresh
- **Fresh:** an eval set of tasks dated after the training data's cutoff, never used for training or tuning, and never reused after it has gated a change (ADR-0006 G).
- **The bar, fixed in this ADR before any run.** A trained model replaces the current one only if all of these hold:
  - its Fresh pass rate beats the current model's by more than the variance floor, from at least 3 base runs (ADR-0006 G);
  - it does so on the same tasks, at a matched budget;
  - no regression on Smoke;
  - and a paired test (per instance, McNemar) at p < 0.05.

  The exact numbers (the floor, n, and the pass-rate delta) are written into `docs/experiments/` before the first training run, and are not changed after it.

### 5. The sequence
Each step needs the one before it:
1. **Smoke baseline:** the current local model on Smoke. Pass rate, cost and edit-tool errors (measurement B of the omp research).
2. **Best-of-n with the verifier choosing:** run n samples per task and let the verifier pick. This is the matched-budget parallel-sampling baseline that training must beat (ADR-0006 G).
3. **Train-split collection:** local-model runs on train-split tasks, exporting verified passes only (part 3).
4. **QLoRA SFT** on the export, on the tower. Then the Fresh bar (part 4), with the best-of-n baseline as the arm to beat.

## Consequences
- **What goes into training:** the local model trains only on its own passes, judged by the verifier, on tasks that can't appear in its evaluation.
- **What can't go into training:** a hosted model's output, an eval task, or an unverified pass. The exporter is the single gate, and it fails closed.
- **What "it improved" means:** the bar is written down before the first run, so the result can't move it.

## Alternatives considered
- **Distil from hosted models:** not adopted. Hosted runs stay `eval-only`, under the private-repo rule and the design's provenance rule.
- **Train on every pass the worker reports:** not adopted. Test results come only from the verifier.
- **RL on weights:** not adopted (ADR-0006 I).
