# ADR 0004: Worker loop and verification

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M3

## Context
The first end-to-end tier needs a worker turn that uses tools, a verification step that is the sole judge of progress, and explicit exits. The spec requires typed worker outputs, tests as ground truth, bounded loops, and a gate that never overrides a failing check.

## Decisions
1. **A turn ends with a separate structured call.** The worker uses tools until it stops, hits the tool cap (10 in T0) or the model-call cap; then one more call, with the whole transcript and a fixed summary instruction, returns `WorkerOutput` through the validate-and-repair loop. Parsing JSON out of free text at the end of a tool-using turn was rejected: constrained decoding on a dedicated call is reliable and the extra call is cheap relative to the turn.
2. **Append-only transcript, size-triggered trimming.** Messages are never deleted or reordered; old tool results are replaced by a placeholder when the transcript exceeds a character budget, keeping the last three intact. This keeps the cached prefix stable and keeps the harness compatible with preserved-thinking checks. Model-driven compaction (Haiku) is deferred to M4/M5 when transcripts get long enough to need it.
3. **Verify has a fixed order and reports every layer.** (a) the working tree must differ from the base commit, (b) feature tests must exist and pass, (c) the whole suite must pass, (d) lint must be clean. A feature's `test_filter` comes from the plan (M4) or, in T0, from the worker's `tests_added`; "no tests collected" is a failure, so an agent cannot pass by writing no tests. The output of every layer is stored as one artifact that the next worker turn sees as `[context from verify]`.
4. **The gate is advisory on top of ground truth.** If any feature is failing, a `pass` verdict is rewritten to `escalate_human` in code, with the override recorded in the reasoning. The research basis for the gate is the objective check, not a second opinion on tests.
5. **Exit reasons are state, routing is pure.** Nodes write `exit_reason`; conditional edges only read state. `success` is set only by `merge_wait` after the human decision, so a run that ends any other way is never reported as a success.
6. **Non-T0 routing halts in M3.** The router runs on every task and its decision is logged; tiers that do not exist yet stop for a human rather than silently running T0.

## Consequences
- One extra model call per worker turn (the summary). Measured on the Fake provider at real-model prices, a full T0 run of t001 is 13 calls.
- `tests_added` is a T0 convenience; M4 moves test ownership to the plan (`PlanStep.feature_ids` → `FeatureItem.test_filter`).
- The gate prompt sees docs-check results only through the PR body; a direct docs-check input to the gate is a small M4 follow-up.

## Amendment (2026-11-14, issue #121): a plan is grounded in the repository before a step runs
In graphcalc P26 (attempts 3 and 4) the T1 planner ran before its explorer, said so ("No exploration summaries were provided, so the file paths (graphcalc/evaluator.py, graphcalc/state.py) are guesses"), and named `graphcalc/evaluator.py`, which does not exist (the evaluator is `graphcalc/calc.py`). `localize` then appended the explorer's files to each step (I-151), and the guess stayed in `files_expected`, where it fed plan drift (I-166) and sent the coder looking for a module that is not there.

- **The explorer before the planner on T1.** The T1 path is now `prepare_repo → localize → plan → post_plan → implement`; it was `plan → post_plan → localize → implement`. The planner reads the localize summary through the same `explorer_summaries` input T2's explorers feed. With no plan yet, the explorer is asked where the task (title and acceptance lines) is implemented and tested, for the existing files in `relevant_files` with `path:line` citations. `localize` still runs once per run: replans (the advisor's, and the gate's, I-043) return to `plan`, never to `localize`. A checkpoint from before this change that is paused between `post_plan` and `localize` resumes as before: `after_localize` sends a state that already has a plan to `implement`. T2 (`scope → explore → plan`) and T0 (no planner) are unchanged.
- **Grounding** (`planning.ground_plan`), applied to every new plan on T1 and T2: the planner's, every replan, and the advisor's `revised_steps`. Each open step's `files_expected` is checked against the files git sees, tracked and untracked. A path stays when:
  - it exists (a file, or a directory that holds one), or
  - the step declares it in the new `PlanStep.files_new` (defaulted to `[]`, so older plans and checkpoints load), or
  - it is a test path (the toolchain's `is_test_path`) in a step whose text says it writes something (add, create, write, new, test).

  Any other path is a guess. It is replaced by the obvious file it meant, when there is exactly one (each match stands in for one guess only):
  1. the one file in the tree whose path ends with it;
  2. the one localized file with the same basename;
  3. the one localized file in the same directory that the step does not list already.

  With no such match, the path is kept as a probable new file when its directory exists (the repository root counts), and dropped only when its directory does not exist either. The reason: an expected file that is never touched causes no drift, but a dropped file that the coder then creates is drift, the S8 halt pattern. P26 attempt 4 created the guessed `graphcalc/state.py`. Each replacement, kept guess or drop is one ledger fact, for example `plan grounding (v1): S1 graphcalc/evaluator.py -> graphcalc/calc.py (localized)` or `plan grounding (v1): S1 kept graphcalc/state.py (probable new file: its directory exists)`. `files_new` entries join `files_expected`, because drift reads only that list.
- **Then T1's localized files join the steps** (I-151), as before, but after the grounded plan. The files that join are the explorer's tracked files outside `.scio/`, docs (`*.md`) and dependency or lock files, whatever the toolchain calls source, so a Python product's `static/app.js` counts (I-159). A step whose every named file was dropped still receives them. Otherwise its empty list would turn the drift check off. Drift therefore compares the diff with the grounded list (I-166).
- **Test filters are flagged, not dropped** (`planning.unmatched_test_filters`). An open feature's `test_filter` is flagged in the ledger when it matches nothing:
  - a path filter, when the path neither exists nor is named by its step;
  - a term filter, when no term is in a test file's path or text (`git grep`) and its step creates no test file.

  The coder may write the test. The note lets a later halt on "no tests collected" be read against the plan.
- **The planner prompt** (`planner` v3) says to name only files the summaries show exist or files the step creates, to list created files in `files_new`, and to give each feature a test filter that selects real tests or tests its step writes.
