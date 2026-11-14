# ADR 0005: Escalation policy implementation

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M4

## Context
ADR 0001 fixed the policy: a Sonnet worker escalates to an Opus advisor on specific signals, with caps, and the advisor's output is the authoritative plan revision. M4 makes it executable. The spec lists six triggers, a never-escalate list, two fixed touchpoints, caps of one call per step and K per run, and logging of every call with trigger, cost and outcome.

## Decisions
1. **Triage is a pure function stored in state.** `scio.loop.escalation.decide(state, cfg)` returns a `TriageDecision` (route, triggers, reason). The `verify` node stores it after a failing check; the `triage` conditional edge only reads it. Routing never computes, every decision is visible in the ledger and the checkpoint, and the six triggers are unit-testable without a graph.
2. **Signals, as implemented.** (1) the same error signature seen `repeat_threshold` (3) times; (2) `stall_count > max_stall` (2); (3) the worker's `confidence == "low"` or `needs_design_decision`; (4) files touched outside the step's `files_expected`, with `tests/` and `test_*.py` always allowed; (5) `sample_disagreement` set by a sampling step (config `sample_n`, off by default, see open question 3); (6) high-impact flags from a textual diff assessment (dependency files, migrations/schema, removed public `def`/`class`, more than N files), raised once per run. Never escalate: lint-only failures, and any failure with no signal, which is the ordinary `debug` path.
3. **Caps and the cap exit.** `Budget.advisor_allowed(step)` enforces one call per step and K per run. A triggered escalation that the cap refuses routes to `halt` with exit `advisor_cap`, which interrupts for a human like `escalate_human`.
4. **The advisor sees a brief, not history.** Goal, step, triggers, impact flags, diff stat, last error head, attempt count, the worker's own summary: about two thousand tokens, stored as an artifact. Its verdict routes: `continue` and `change_approach` go back to `implement` with the guidance injected as `[context from advisor]`; `replan` with `revised_steps` installs a new plan version authored by the advisor, else re-runs `plan` with the guidance as the revision reason; `escalate_human` halts.
5. **Outcome is decided by the next verify.** An escalation is `pending` until the next verify: passing marks it `resolved`, failing marks it `unresolved`. Tokens and dollars come from the cost ledger delta around the call.
6. **One writer, parallel readers.** T2 explorers run through LangGraph `Send` with a read-only toolbox (`repo_search`, `view_file`) and reduce into `explorer_summaries`; every edit happens in a `coder`, `debugger` or `documenter` toolbox. Spans carry the role so the invariant is asserted from traces, not assumed.
7. **Steps advance at checkpoint time.** `verify` marks a step done; `git_checkpoint` commits that step's passing features and moves `current_step_id` to the next open step. Evaluating the step before advancing is what makes the per-step commit and the per-step advisor cap correct.
8. **ADRs in the target repository.** When an advisor returned `replan` or `change_approach`, or the diff is high impact, the Documenter writes `docs/adr/NNNN-<slug>.md` in the target repository if it keeps ADRs there, before the docs commit.

## Consequences
- Thresholds (3, 2, 8, K per tier) live in `configs/budgets.yaml` under `escalation` and are the M5 tuning surface.
- Plan drift is only evaluated after a failing verify. A drifted patch that passes every check is accepted; the Reviewer and the gate see the diff. Revisit if evals show silent scope creep.
- Patch sampling (signal 5) is wired but off by default; the sampling step itself is an M5 item measured against its token cost.

## Amendment (2026-11-14, issue #116): a capped session that edited nothing climbs a ladder before it can stall
In the graphcalc run, P26 attempt 3's T1 coder session (Claude Code backend) spent its whole turn cap on a correct diagnosis: `33 tool calls, stop=tool_cap`, `files_touched: []`, 17 minutes. SCIO then started a fresh debugger session, which redid the reconnaissance before it edited. The worker's stop reached only a ledger line, so `verify` and `triage` could not tell a session that ran out of turns from one that chose to stop, and the empty iteration counted toward a stall.

- **The state records it (the contract).** `LoopResult.session_id` names the Claude Code session (None for `AgentLoop`). `ProgressLedger` gains `last_worker_stop`, `last_worker_edited`, `last_worker_resumed`, `raised_cap_step`, `raised_cap_pending` and `stall_exempt`, all with defaults, so older checkpoints load.
- **Edits are judged from git, never from the worker's report.** `implement.tree_snapshot` writes a git tree of every file git does not ignore, through a throwaway index (`GIT_INDEX_FILE`), so the repository's own index is untouched. The snapshot is taken before the session, after its first call, and after a resume. `changed_since_head` cannot answer the question: on a step's second attempt the first session's uncommitted edits are still in the tree. The toolchain's cache excludes (`prepare_repo`) keep a test run from counting as an edit. When git cannot answer, the ladder does nothing.
- **The ladder (the owner's order, cheapest step first), in `implement` and `debug`.** A session is capped when its stop is `tool_cap` (Claude Code's `--max-turns`, SCIO's tool cap) or `model_cap` (SCIO's own loop on T1/T2). A capped session with no edit takes the next unused step:
  1. **Continue the same session.** `ClaudeCodeWorker.resume_typed` runs `claude -p --resume <session>` with every flag again (`--permission-mode` is not inherited on a `-p` resume) and no system prompt file (the session keeps it). It runs for `RESUME_TURN_CAP` = 10 turns: the longest measured edit span (7 turns), plus a test run and a closing turn. The prompt points at the summary the session just wrote ("You have diagnosed it; now write the fix"). The typed summary is then taken again. This happens inside the same node, before verify. The `aci` backend has no session, so it skips this step.
  2. **Carry the findings forward.** Unchanged: I-154, `previous_on_step`.
  3. **More turns.** If the resumed session still edited nothing, the next coder or debugger session on the same plan step gets `RAISED_CAP_FACTOR` = 2 times the role's cap. That is 60 Claude Code turns at T1/T2, or 24 model calls on `aci`. It is granted once per step, keyed `v<plan version>:<step id>` so a replan's reused ids start afresh. The iteration that earned it counts no stall (ADR 0006 amendment).
  4. **Then halt as before.** If the raised-cap session also ends capped with no edit, after its own resume, stall counting proceeds as before.
- **Measured before relying on it** (Claude Code 2.1.325, Haiku, 2026-11-14, on #116):
  - A `-p --resume` keeps the session id and runs tools when the tool flags are passed again. It does so even after the no-tools `--json-schema` summary call: a capped call, then the summary, then a tool resume finished the remaining steps.
  - `--max-turns` and `num_turns` count per call.
  - `duration_api_ms` and `total_cost_usd` are cumulative over the session. Each call's ledger latency is now its own share (I-210). The `usage` block, which SCIO prices, was always per call.
  - The tool resume read the cached prefix (74,496 cache-read tokens) although the summary call before it read none.
- **Out of scope, possible later steps.**
  - Moving a step up a tier mid-feature (T0 to T1, T1 to T2) instead of raising its cap.
  - T0's `t0_loop`, which keeps today's behaviour: its capped-empty sessions still count toward a stall.
