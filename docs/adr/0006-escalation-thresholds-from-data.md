# ADR 0006: Escalation thresholds set from the tuning sweep

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M5

## Context
ADR 0005 put the escalation thresholds in `configs/budgets.yaml` as the tuning surface. M5 adds `evals/tune.py`, which sweeps `repeat_threshold` × `max_stall` over the capability scenarios and reports pass rate, tokens, dollars, advisor-call rate and resolution rate per cell. The account has no Anthropic API key (ledger I-019, placeholder by decision), so the sweep ran on the Fake provider at real-model prices.

## The data (Fake provider, 2 trials, scenarios c001 repeated-error and c002 plan-drift)

| repeat_threshold | max_stall | pass | tokens | $ | advisor calls | resolved |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 1 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 2 | 2 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 2 | 3 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 3 | 1 | 0.50 | 23676 | 0.0555 | 0.50 | 1.00 |
| 3 | 2 | 1.00 | 29762 | 0.0703 | 1.00 | 1.00 |
| 3 | 3 | 1.00 | 29762 | 0.0703 | 1.00 | 1.00 |
| 4 | 1 | 0.50 | 23676 | 0.0555 | 0.50 | 1.00 |
| 4 | 2 | 0.50 | 26830 | 0.0624 | 0.50 | 1.00 |
| 4 | 3 | 1.00 | 32918 | 0.0773 | 1.00 | 1.00 |

The sweep's own rule ("cheapest cell with pass rate 1.0") picks `repeat_threshold=2, max_stall=1`.

## What the data can and cannot say
- **It measures control flow, not model quality.** The Fake debugger never fixes anything, so every extra debug turn before escalation is pure cost. Escalating on the second identical failure is cheapest *only* because the retry can never succeed. With a real model the first debug turn often fixes a clear-traceback failure, and the research basis (RESEARCH.md: never escalate for clear-traceback failures; advisor calls capped) argues for giving it that chance.
- **It does say something firm about stalls.** Every cell with `max_stall < repeat_threshold - 1` fails half the runs: the stall exit fires before the repeated-error signal can escalate. The two thresholds are coupled: `max_stall >= repeat_threshold - 1`.
- **It bounds the cost of waiting.** Going from threshold 2 to 3 costs about 11% more tokens on a run that does escalate (26.6k → 29.8k), which is the price of one extra debug turn.

## Decision
1. `repeat_threshold = 3` (the spec's "same error after 2 fix attempts"), `max_stall = 2`. The Fake data confirms this cell passes at 1.0 and resolves every escalation; the 11% premium over threshold 2 is the budget reserved for a real debugger's first fix attempt.
2. The coupling `max_stall >= repeat_threshold - 1` is now enforced: `EscalationConfig` raises on a violating configuration, and the sweep grid skips those cells.
3. `advisor_calls_max` stays 3 (T1) and 6 (T2): no scenario needed more than one call per step.
4. Re-run `python -m evals.tune --provider anthropic` when a key exists and revisit decision 1 with real resolution rates. Until then the thresholds are "spec defaults, confirmed safe by control-flow data", not "optimal".

## Live data (2026-10-12, issue #31)
The first live debugger run (t009, `sonnet_opus_advisor`, pinned T1) is the data point decision 1 reserved budget for: verify failed on a lint-only error in a module no test imports, the debugger fixed it in one turn (9 calls, $0.06 of a $0.34 run, with a repository sweep), and the second verify passed. No escalation signal fired. This supports keeping `repeat_threshold = 3`: the first debug turn did fix a clear, local failure, which the Fake debugger structurally could not. It is one run on one failure kind; decision 4 still stands, and a live sweep still needs a scenario in which the debugger fails repeatedly (the capability tasks are `providers: [fake]`), which no live task produces yet.

Update (issue #37, ADR 0008): the first live advisor calls happened in one t010 run. One was the new pre-commit review of a passing high-impact diff (verdict `continue`), which is not failure data. The other was a natural failure-path escalation: step S2 failed verify on a planned `test_filter` that matched no test, `low_confidence, plan_drift` routed to `advise`, Opus answered `continue`, and the next verify resolved it. That is the first live resolution-rate data point (1 of 1); decision 4 still waits for repeated-error data, which needs a debugger that fails three times on one signature.

## Consequences
- The sweep is reproducible from `evals/tune.py` and its JSON/Markdown reports live under `evals/results/` (git-ignored; the nightly workflow uploads them as artifacts).
- A future live sweep must include a scenario where the debugger *can* succeed, or it will reproduce the same bias toward early escalation.
- Status 2026-10-12: decision 4 is still pending, now for a different reason than the key. Across 31 live task rows (three nightly regression suites and the first trials of t006 and t007) no run made an advisor call, so the sweep has nothing to measure; it waits for a live task that fails verify at least twice (`docs/IDEAS_TRIAGE.md`, status note).

## Amendment (2026-11-14, issue #116): one stall-exempt iteration per plan step
The capped-session ladder (ADR 0005 amendment of the same date) adds one exemption to stall counting. Take an iteration whose worker session ended at its turn cap with no edit in git, was resumed once where the backend has sessions, still edited nothing, and so earned the step's raised turn cap. That iteration does not count toward a stall. `_after_worker` sets `ProgressLedger.stall_exempt` on it. The next `advance` then leaves `stall_count` where it was when the fingerprint is unchanged, and clears the flag. The raised cap is granted once per plan step, so a step has at most one exempt iteration. A later capped session that edited nothing counts as before.

- **The invariant holds.** `max_stall >= repeat_threshold - 1` is still enforced by `EscalationConfig`, and the thresholds (3 and 2) are unchanged.
- **The exempt iteration is neither a stall nor a fix attempt.** It changed nothing, so the same error signature does not count one more `fix_attempts_on_error` either. A new signature still starts at 1. The iteration and the fingerprint still advance. Both signals pause for the same one iteration, so their order is unchanged: the repeated-error signal still fires no later than the stall exit, which is the ordering the invariant protects.
- **Why the fix-attempt count pauses too.** The agent's first draft counted it. On a step whose one advisor call was spent, that let `decide` halt with `advisor_cap` before the raised-cap session ran, against the owner's step 4, "halt only after that fails". The orchestrator changed it before the PR. `test_an_exempt_iteration_is_no_fix_attempt_so_advisor_cap_waits_for_the_raised_cap` guards it. Since issue #172 (ADR 0005 amendment, 2026-11-17) a spent step call halts only when the run's total is spent too; the pause still matters then. The turn after a used consult is an ordinary fix attempt and stall candidate, so neither count pauses for it.
- **The run stays bounded.** `max_iters` still bounds every run, and the ladder adds at most one exempt iteration per step.
- **What a halt means now.** A `stall` halt now means the step is stuck. It no longer means a session ran out of turns while reading.
