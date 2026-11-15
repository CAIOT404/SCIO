# Lessons learned

One section per milestone, written when the milestone closes. **Read this whole file before starting a milestone** and say which lessons shape the plan. Every "action" below is something that exists (a file, a test, a rule, a ledger id), not an intention. Problems referenced as `I-NNN` are in the ledger, one file each in [docs/ledger/](ledger/README.md).

## Guardrails (Always / Never)

Every bug and every correction becomes one rule here (the owner's rule, 2026-10-27). A rule starts with **Always** or **Never**, cites its ledger id (or the correction it came from), and names its **guard**: what fails when the rule is broken. A rule is the index; the guard is what stops drift. Prefer, in order: a structural fix that makes the wrong thing impossible (`symbol:`), a regression test shown to fail on the old code (`test:`), a scan of the whole codebase (`test:` or `check:`), a Claude Code hook (`hook:`), and `manual:` with the reason only when nothing can check it. `scripts/check_docs.py::check_guardrails` fails when a resolved ledger entry from I-104 on is cited by no rule, or when a named guard does not exist.

**The rules are files**, one per rule, named by its number: `docs/guardrails/NNN.md`, three digits (issue #122, W-39). Each holds its one rule line, as it would read in a numbered list here. [guardrails/](guardrails/) holds them: `cat docs/guardrails/*.md` prints them in order, and `uv run python scripts/next_ids.py` gives the next number. `check_guardrails` reads them there, and fails on a rule written into this section.

## Standing pre-flight list (apply at the start of every milestone)

1. Read this file and the open entries in the issue ledger.
2. Check the API reference before any provider code; the spec was written against an older API surface in three places (see M0).
3. Confirm the `gh` token scopes cover what the milestone will push (`workflow` for CI files) before creating the branch (I-007).
4. Run the full local CI-equivalent (command in `CLAUDE.md`) before every push, not just the tests. A check that passes on Windows can still fail on Linux (I-011): any check touching paths needs a unit test with explicit inputs, not only a run against the repo.
5. Write files with LF endings; pass `newline="\n"` from Python on Windows (I-006).
6. One heredoc per shell command when the body contains quotes, and never pass any backslash through a shell-embedded script: escapes (`\n`, `\t`), Windows paths, regexes; use the file tools for those edits (I-001, I-010, I-027, I-036). After any shell-embedded patch, run the lint step or `python -c "import scio"` in the same command, and treat a `SyntaxWarning: invalid escape sequence` in the patch output as a failure, because Markdown has no lint to catch it.
7. Add dependencies to `pyproject.toml` and run `uv sync`; never `uv pip install` ad hoc. Export `UV_SYSTEM_CERTS=1` once per session (I-012).
8. Introspect an installed SDK (`inspect.signature`, `dir`) before writing an adapter against it; write the adapter's tests with an injected client or runner so they run without the service (M2).
9. Before pushing, compare what git will send with what the working tree has: `git status --short` must show every new package, and `git ls-files --others --ignored --exclude-standard scio evals tests scripts` must be empty (I-017). The docs check enforces the second. Note that ruff skips git-ignored files, so an ignored package is also an unlinted package.
10. When chaining checks in one shell command, start with `set -o pipefail` and never pipe a check whose exit code matters through `tail`/`grep` without it (I-018). A chain that reaches `git commit` must have been able to fail.
11. Commands that must not run after a failed check go on the same `&&` line; a heredoc's `&&` continuation goes on the opener line, because the line after `EOF` is a new command. Do not pass `-q` to pytest: `addopts` already does, and `-qq` hides the summary (I-023).
12. When adding a keyword argument that every call site should pass (a role tag, an intent), grep for every existing call site in the same change; a default value hides the ones you missed (I-026). When a node writes a value that a following edge reads, the node must not also advance past it (I-025).
13. Anything that refreshes a rotating credential (the login profile's refresh token) must exist once per process: share the SDK client across parallel trials, never build one per harness (I-044). A fan-out that works with a fresh token and fails hours later is this bug. A scheduled task registered with `schtasks` has no catch-up or battery conditions; register it from PowerShell with `StartWhenAvailable` and `WakeToRun`, and treat a missing history row as the alarm (I-045).
14. A model's structured output never names control-flow identity (the current step, the feature in scope); it reports, the plan decides (I-046). Every loop in the graph needs a cap on the passing path as well as the failing one: a pass that changes nothing is a stall. When a diagram says an edge exists (`gate --replan--> plan`), test that the code has it (I-043); the verdict list of every structured output is a list of edges to check.
15. A node that interrupts does nothing else: a node re-runs from its first statement when the graph resumes, so a model call (or any effect without a probe) before `interrupt()` in the same node runs twice and the human answers a draft they never saw (I-065). Put the stop in its own node that reads from state, and test it on the Fake with a responder whose answer changes on every call.

---

## M0: design (closed 2026-10-09)

### What succeeded
- Reading the current API reference before designing caught three mismatches between the spec and reality: Opus 5.5 advisor output is encrypted (so the native advisor tool cannot be logged for outcome, assumption F-5), Opus 5.5 defaults to `medium` effort (F-4), and the spec names previous-generation Sonnet/Haiku models (F-2, F-3). Each became a numbered assumption instead of a silent choice.
- Numbering assumptions (F-1 to F-12) and open questions with the milestone that must answer them gave M1 a checklist instead of a vague "resolve later".
- Starting the repo from an issue, labels, CODEOWNERS and templates on day one meant M1 could follow CONTRIBUTING from its first commit.

### What failed
- Branch protection is not available on the private free-plan repo (I-009). The "no direct push" rule is convention only.

### Actions
- `docs/ARCHITECTURE.md` section (f): assumptions F-1 to F-12, open questions 1 to 8.
- Issue #1 opened from the feature template with M1 done criteria.

---

## M1: harness, prompt layer, evals skeleton, CI (closed 2026-10-10)

### What succeeded
- Writing the tests in the same pass as the code, then running the CI-equivalent locally, caught every defect before the first push: 2 test failures, 3 type errors, 80 lint findings, 6 line-ending problems. Nothing reached CI broken.
- The Fake provider as a first-class adapter made the whole harness, the repair loop and the eval runner testable offline, with cost still computed at real-model prices.
- Translation-only unit tests for the Anthropic and OpenAI adapters (request kwargs in, response objects out) gave coverage of the SDK boundary without network, while live tests stay gated behind `SCIO_RUN_LIVE_TESTS=1`.
- `scripts/check_docs.py` enforcing "model IDs only in `models.yaml`" turned a design rule into a failing check.

### What failed
- A twelve-heredoc shell command aborted and wrote nothing (I-001). Cost: one extra round of writes.
- `configure_tracing` was written as if the OTel global could be replaced per call (I-003). Cost: two failing tests; the fix also changed the test fixture design.
- Patching files from Python on Windows silently produced CRLF in six files (I-006). The sweep found one production write (`evals/report.py`) with the same latent problem.
- The push was rejected for a missing `workflow` token scope after all the work was done (I-007). Scope should have been checked before the branch was created.
- Protocols for the ledger, retrospectives and sweeps were not in place from the start; this file and the ledger were written retroactively at the end of M1.

### Actions
- `CLAUDE.md` (new): the three continuous-improvement protocols plus engineering rules, loaded in every AI session.
- `CONTRIBUTING.md`: "Continuous improvement protocols" and "File hygiene" sections.
- `.github/pull_request_template.md`: ledger ids, sweep pattern and retrospective checkboxes.
- `scio/prompt/templates/debugger/v2.md`: the Debugger role performs a codebase sweep after every root cause.
- `docs/ISSUE_LEDGER.md` (new): entries I-001 to I-011.
- `evals/report.py`: explicit `newline="\n"` on file writes.
- Pre-flight list above, items 3, 5 and 6.

### Carried into M2
- I-008 (pdoc warning) is open; I-007 (token scope) and I-011 (link checker) were closed after the first CI run.
- The `WorkerOutput` structured schema (M2 state) must gain a `sweep` field so the Debugger's codebase sweep is recorded in state and the PR body, not only in prose.
- Open questions 2 and 3 in ARCHITECTURE.md (sandbox in CI, patch sampling N) are due in M2.

---

## M2: state, checkpointing, ACI tools, sandbox, GitHub client (closed 2026-10-10)

### Lessons applied from M1
- Pre-flight item 3: token scope checked before branching (no push problems this time).
- Pre-flight items 5 and 6: every patch script avoided backslash escapes; hand edits with escapes went through the file tools. Zero CRLF files, zero corrupted patches.
- Pre-flight item 4 (platform differences): the sandbox path rules got explicit Windows and POSIX unit tests instead of only a repo run.
- Carried item: `WorkerOutput.sweep` exists and is tested.

### What succeeded
- Introspecting the installed LangGraph and E2B packages before writing the adapters (signatures of `invoke`, `task`, `interrupt`, `Sandbox.create`, `commands.run`, `files.*`) meant no API was written from memory.
- The two-layer idempotency design (effect log plus world probe) passed the crash-resume test at both crash points on the first run.
- Injected runners for Docker and `gh`, and a fake client for E2B, gave full coverage of command construction without any daemon, key or network.
- The ACI tools were testable end to end on a throwaway repo with real pytest, ruff and git, so "edit rejected by lint" and "failures only" are verified behaviour, not intent.

### What failed
- Installing packages ad hoc with `uv pip install` re-triggered the TLS failure (I-012); the M1 fix had been applied to one command, not to the environment.
- A test double parsed nested branch names wrongly (I-013): the double was less careful than the code under test.
- The error signature preferred the source assertion over pytest's evaluated one (I-014): a "first match wins" rule chose the wrong line.
- Platform-conditional stdlib typing (I-015) cost one mypy round.
- Docker is not installed on the dev machine and there is no E2B key, so two backends are verified structurally only (I-016).
- The whole `scio/aci/sandbox/` package was silently excluded from the commit by an unanchored `sandbox/` line in `.gitignore` (I-017). Local checks run on the working tree and could not see it; CI could.

### Actions
- `scripts/check_docs.py::check_ignored_sources` fails when any source file under the code directories is git-ignored (I-017). Pre-flight item 9 below.
- `pyproject.toml` owns every dependency; `UV_SYSTEM_CERTS=1` is in CLAUDE.md as the environment rule (I-012).
- `docs/adr/0003-idempotent-effects-and-sandbox-tiers.md` records the durability and sandbox decisions.
- `tests/durability/test_crash_resume.py` is the executable acceptance criterion for crash safety and runs in CI.
- Ledger I-012 to I-016.

### Carried into M3
- I-016 open: run the gated Docker and E2B live tests when a suitable machine is available.
- Register the `t0` graph in `scio.graph.registry` and replace the hand-drawn T0 diagram in ARCHITECTURE.md with a generated section.
- Open question 3 (patch sampling N) is deferred to M4 where sampling is built.
- Every side-effecting node must name an `intent` and a `probe`; make that the node template shape from the first M3 node.

---

## M3: router and T0 loop end to end (closed 2026-10-10)

### Lessons applied from M2
- Pre-flight 9 and 10: `git status` reviewed before the push; every check chain started with `set -o pipefail`. No ignored packages, no masked failures.
- Carried items: `t0` is registered and its generated diagram sits next to the M0 sketch; every side-effecting node names an intent and a probe.
- Pre-flight 8: LangGraph's `interrupt`/`Command` and the serializer constructor were introspected before use.

### What succeeded
- The whole T0 graph ran end to end on the first smoke attempt: route, clone, implement, verify (real pytest, real ruff), commit, document, gate, interrupt, resume, hidden tests green. The layered design from M1/M2 meant the graph was mostly wiring.
- A schema-keyed `ScriptedResponder` (decide by the requested output schema, then by role and turn count) made one fake drive the entire run and every exit-reason variant with small flags.
- Verify's fixed order (changes → feature tests → suite → lint) caught the "trivially passing" hole during design: a whole-suite check alone would have passed an agent that wrote nothing.

### What failed
- The recorded live run is blocked by an invalid API key in the environment (I-019). The code path is exercised to the first request only.
- Project tooling linted and type-checked the agent's fixture repository (I-020).
- LangGraph's serializer allow-list was unknown until the first checkpoint warning (I-021).
- Two queue-based loop tests enqueued the wrong number of scripted replies (I-022).

### Actions
- `docs/adr/0004-loop-and-verification.md`.
- `scio.graph.checkpointer.serializer()` allow-lists every state type; the Postgres path is flagged for the same treatment.
- `pyproject.toml` excludes `evals/tasks/*/repo` from ruff and `evals/tasks/` from mypy.
- `tests/graph/test_t0_fake.py` is the executable acceptance for the happy path and all five reachable exit reasons; `tests/graph/test_t0_fake.py::test_every_exit_reason_is_covered` fails if a new `ExitReason` is added without a test.
- Ledger I-019 to I-023.

### Carried into M4
- I-019: run `python -m evals.runner --suite regression --provider anthropic --trials 5` once a valid key is present; record pass^5, tokens and cost here and in the PR.
- T1/T2: `after_route` currently halts non-T0 tiers; M4 replaces that with the plan/explore subgraphs and the six-signal `triage` edge. `check_exit` already returns `ADVISOR_CAP`'s neighbours; the advisor cap exit is added with the `advise` node.
- Move test ownership from `tests_added` to the plan (`PlanStep.feature_ids` → `FeatureItem.test_filter`).
- Postgres checkpointer: construct with the serializer allow-list (I-021 sweep).
- The gate should receive docs-check output directly, not only through the PR body (ADR 0004 consequence).

---

## M4: T1/T2 tiers and the escalation policy (closed 2026-10-10)

### Lessons applied from M3
- All five carried items closed: T1/T2 replace the halt; the plan owns `test_filter` (the planner must set one per feature; `tests_added` remains the T0 fallback); the Postgres saver is built with the serializer allow-list; the gate runs the docs check itself; `advisor_cap` is an exit and interrupts for a human.
- Pre-flight 11: every chained check started with `set -o pipefail`, pytest ran without `-q`, and no command followed a heredoc on its own line. No masked failures this milestone.

### What succeeded
- Making triage a pure function that `verify` stores and the edge reads gave the six-signal policy a unit-test surface with no graph at all: one flip test and one non-flip test per signal, the never-escalate list, both caps, and the exit precedence.
- One builder for all tiers (`build_graph(ctx, tier)`) kept T0 byte-for-byte reusable: the M3 tests passed unchanged on the unified graph before any T1/T2 test was written.
- A data-driven fake (`TaskScript` per task, schema-keyed replies) drove five real eval tasks across all three tiers and every escalation scenario with four switches.
- Span attributes made the one-writer invariant an assertion over traces rather than a claim in prose.

### What failed
- The fake answered the router with a plan because marker detection was ordered wrongly (I-024).
- `verify` advanced the step cursor before the edge evaluated it, and `git_checkpoint` stamped every feature with the first commit (I-025). Two separate defects behind one symptom; the second only appeared after the first was fixed.
- Writer toolboxes created in M3 were untagged, so the trace assertion saw `agent` (I-026). A new keyword argument added in M4 was not applied to the call sites that predated it.

### Actions
- `docs/adr/0005-escalation-policy.md`: signals as implemented, caps, brief, outcome rule, step advancement at checkpoint time, ADRs in the target repository.
- `tests/unit/test_escalation.py` (six flip / non-flip pairs, never-escalate, caps) and `tests/graph/test_t1_t2_fake.py` (T1 happy paths, repeated-error escalation resolved, advisor cap interrupt, plan drift with a generated ADR, advisor `escalate_human`, T2 parallel explorers with one writer, T0 docs task via the router).
- Ledger I-024 to I-026. Pre-flight item 12 below.

### Carried into M5
- I-019 still blocks the live Anthropic acceptance run; M5's A/B configs need it.
- Tune `configs/budgets.yaml` `escalation` thresholds and `advisor_calls_max` from eval data; decide whether to enable `sample_n > 1` (open question 3) after measuring its token cost.
- Plan drift is evaluated only after a failing verify (ADR 0005 consequence); check the capability suite for silent scope creep on passing runs.
- Nightly capability suite and release workflow (M5 scope).

---

## M5: observability, eval tuning, release (closed 2026-10-10)

### Lessons applied from M4
- I-019 was closed as a decision, not a fix: the owner is on a Max subscription, so the key stays a placeholder and every measurement is labelled Fake-provider data. The nightly workflow switches to the real provider on its own when a repository secret appears.
- Thresholds were set from a recorded sweep (ADR 0006), with the data's limits written down next to the numbers.
- Pre-flight 12: the new `ToolBox`-style keyword (`scenario`, `fixture`) was applied to the runner, the solver and the task files in the same change.

### What succeeded
- The first run against a real GitHub repository worked end to end on the first attempt: issue intake, branch push, draft PR with the template and `Closes #1`, docs in the same PR, gate, ready, and that repository's CI green.
- The tuning sweep produced an actionable result and, more usefully, a constraint (`max_stall >= repeat_threshold - 1`) that is now enforced in code.
- The report builds from durable state and a persisted ledger, so observability does not depend on the process that ran the task.

### What failed
- The shell tool unescaped `\n` inside two patch scripts and corrupted two modules (I-027), a recurrence of I-010 despite the rule. The rule was not enough; the guard is now mechanical (lint or import immediately after any shell-embedded patch).
- The real PR committed `__pycache__` files (I-028). Every Fake-provider test counted commits and passed; only the real PR's file list showed it. Tests now assert the tracked file list.

### Actions
- `docs/adr/0006-escalation-thresholds-from-data.md`; `EscalationConfig.__post_init__` enforces the coupling; `evals/tune.py` skips invalid cells.
- `prepare_repo` writes `.git/info/exclude`; `test_happy_path_end_to_end` asserts no cache files are tracked.
- Pre-flight item 6 extended with the mechanical check; ledger I-027, I-028.
- README acceptance table with evidence links; CHANGELOG cut to 0.1.0; release workflow.

### Live validation (2026-10-11, after 0.1.0)
The first runs with a real model found five problems the Fake provider structurally could not (I-031 to I-035): verify built a `-k` expression out of pytest node ids; a `view_file` on a directory crashed the run; the budget pre-check refused calls with 20% left; bug-fix tasks reported no "added" tests and were told to add some; task budgets were sized for the hinted tier, not the routed one. All five were in the harness, none in the model: the live coder fixed every task in one iteration once verify stopped getting in its way.

Lessons:
- A scripted fake reproduces the *shapes* you imagined. Before trusting a fake, write down the shapes a real model is likely to produce (node ids, empty lists, directories as paths, extra JSON fields) and script those too. The t001 script now reports a node id and a keyword for this reason.
- Keep the working directory of the first live run of every task (`GraphSolver(keep_dirs=True)`) and read the artifacts and the per-role ledger before changing anything. Each diagnosis above took one kept run.
- Run one live trial, read the cost by role, then scale. The whole validation cost about $6.
- Fan trials out (`--parallel`), not tasks: tasks share nothing but the rate limit, and the per-trial ledger stays readable.
- The planner is a model too. Given a task that says "the entry point is not documented", Opus planned an exploration step, which a verify built on "the tree must change" can never pass (I-039). Every structured output that drives control flow (plans, routings, verdicts) needs a normaliser that enforces the invariants the graph assumes, and the prompt should state those invariants in the model's terms ("every step changes code or tests and is provable by tests").
- Live T1 runs are three to five times the cost of T0 runs on the same task (t003: $0.64 vs $0.13 for a T0 docs task of similar size), almost all of it in coder turns that resend their transcript. Context growth per turn, not the number of model roles, is the cost driver to work on next (I-038).

### Realistic tasks (2026-10-12, issue #25)
Two regression tasks were added for the realism gaps in `docs/IDEAS_TRIAGE.md` item 7: t006, a change that spans the store module, the CLI and their tests, and t007, a fixture whose own CI (`ruff` with the repo's rule set, then `pytest`) is red. Both passed their first live trial ($0.35 and $0.40, two iterations each, no escalation).

Lessons:
- Hidden tests can run the fixture's own CI: t007's hidden test shells out to `ruff check .` in the repo root, so the agent is graded on the rules the repository selected, not on verify's narrow syntax-and-undefined-names lint. The live coder removed the unused import without a lint tool, from reading `pyproject.toml` and the workflow.
- Read the gate's non-blocking caveats on every first live run. Both runs passed, and both gates mentioned committed bytecode; that was a real defect in the eval harness (I-048), not a model quirk.
- Never run a fixture's tests inside `evals/tasks/*/repo`. Copy it out first (`make_origin` now drops caches either way). The fixtures are the agent's starting state, and anything on disk there ships with it.

### Snapshot coupling task (2026-10-12, issue #29)
t008 asks for a formatting change whose obvious implementation breaks a CLI snapshot test and a README example test that a filtered `run_tests` never executes. It was built so that verify's full-suite step fails and the debugger runs live for the first time (ADR 0006 decision 4 has never had a live data point). Two live trials on `sonnet_opus_advisor`:

| trial | tier | result | iterations | tool calls | cost | debugger called |
|---|---|---|---:|---:|---:|---|
| 1 (router) | T0 | pass | 2 | 17 | $0.19 | no (T0 has none) |
| 2 (`tier_pin: T1`) | T1 | pass | 2 (one per step) | 23 | $0.39 | no |

What happened instead:
- The router (Haiku) rated the task T0 at confidence 0.70: "the mention of docs and the test suite is minor verification rather than scope expansion". The hint in `task.yaml` only sizes the budget. `tier_pin:` now makes the route deterministic for tasks built around one path (I-050).
- With the pin, the planner split the work into two steps and the coder, in step S1, ran the whole suite itself before summarizing, saw the snapshot test fail, and regenerated `tests/golden/demo.txt` even though the step's file list did not name it ("outside expected files ... needed for full suite to pass"). Verify then passed first time on both steps. In the T0 trial the coder also ran the full suite, stopped after finding the golden file, and fixed it on the next turn.
- So the trap was defused by the coder's own habit of running the full suite, not by verify. The debugger is still live-untested. A task that forces it needs a failure the coder cannot see from inside its own turn: a check verify runs and the coder's `run_tests` does not (lint on the repo's rule set, a hidden-but-declared CI step), or a test that only fails on a fresh checkout.

Lessons:
- A hint is not a pin. Every eval task whose purpose is a specific graph path must state that path in a way the harness enforces; the router is a model and it is told to prefer the cheaper tier.
- Design a "must fail verify" task from what the worker *cannot observe*, not from what the plan's filter omits. Sonnet runs the full suite unprompted; a filtered-out test is not hidden from it.
- Read the cost ledger by role before claiming a node ran. `tier=T1 iterations=2` looked like plan, fail, debug; the ledger had no debugger row and the second iteration was the second step.
- Two causes can hide behind one `ignore_errors=True` (I-049): read-only git objects and an open SQLite handle each blocked the cleanup, and 636 trial directories accumulated silently. Cleanup code logs failures; it never ignores them.

Actions: `tier_pin` (`EvalTask`, `RunContext`, `route`; `tests/unit/test_tier_pin.py`); `remove_tree` and `RunContext.close()` (`test_remove_tree_deletes_read_only_git_objects`, leak assertion in `test_cli_exit_codes`); ledger I-049, I-050; this section. Open: a task that makes verify fail against a full-suite-running coder (candidate: t007's pattern with a lint-only failure in a file the step does not touch).

### Verify-only failure task (2026-10-12, issue #31)
t009 applies the lesson from t008: the failure must be one the coder cannot observe from inside its own turn. The fixture's `metrics/report.py` uses `DECIMALS` without importing it; no test imports that module, so `run_tests` is green at every filter and only verify's lint step (`ruff --select E9,F63,F7,F82,F821` over the tree) fails. The task itself is an unrelated feature (`stdev`). One live trial on `sonnet_opus_advisor`, pinned T1:

| tier | result | iterations | tool calls | cost | debugger called |
|---|---|---:|---:|---:|---|
| T1 | pass, 4 hidden tests | 2 | 14 | $0.34 | yes: 9 calls, $0.06 |

What happened: the coder added `stdev` and its tests in one turn (filters `stats`, `stdev`, `test_stdev` all green, full suite green); verify reported `lint_only`; triage took the default route to debug; the debugger viewed `report.py` and `stats.py`, added `DECIMALS` to the import, searched the repository for the same construct, reran the tests and reported a sweep with the files it ruled out; the second verify passed and the rest of the run (review, docs, gate) was ordinary. The debugger did not delete the module or the rounding (hidden test). First live data point for ADR 0006 decision 4: one debug turn resolved a lint-only failure at about a fifth of the run's cost.

Lessons:
- The reliable way to force a graph path in an eval is to put the failure outside the worker's tools. Here the coder has `run_tests` and no lint tool, so an undefined name in an unimported module is invisible to it and visible to verify. Build path-specific tasks from the tool gap, then pin the tier (I-050).
- The debugger prompt's "sweep the repository for the same construct" produced a real sweep (pattern, files checked, files ruled out) on the first live run; keep the structured `sweep` field, it is the evidence that the fix was not local.
- Hidden tests for a lint-only task must check the *fix*, not just the lint: `format_report([1, 2, 4]) == "mean=2.33 median=2.0"` rules out deleting the module or the rounding, which would also silence F821.
- Hidden tests under `evals/` are linted by SCIO's own ruff config (I-051): write them to that standard, not the fixture's.

Actions: t009 fixture, hidden tests and fake script (`Step.debug_edits`, so the scripted debugger has something to apply when the step was correct); `test_t1_lint_only_failure_runs_the_debugger` asserts the debugger ran at iteration 2 and was shown a lint failure and no test failure; ledger I-051, I-052; this section.

### Advisor review of passing high-impact diffs (2026-10-12, issue #37, ADR 0008)
Issue #37 set out to force the advisor live with a wide rename. A read-only pass over `escalation.py` and `verify.py` before writing any code showed the premise was wrong: every trigger was evaluated only after a failing verify (I-056). The owner chose the design change: a passing verify with unreviewed high-impact flags now gets one pre-commit advisor review; `continue` commits without another coder turn. t010 (rename `format_cents` to `format_money` across nine modules, pinned T1) is the first task built on it. One live trial on `sonnet_opus_advisor`:

| tier | result | iterations | tool calls | cost | advisor calls |
|---|---|---:|---:|---:|---|
| T1 | pass, 3 hidden tests | 3 | 57 | $0.64 | 2, both resolved ($0.08) |

What happened:
- The planner split the work into S1 (`money.py` and its tests) and S2 (migrate the callers). S1's two-file diff passed verify but removed the public `format_cents`, which the impact detector flags on its own, so the ADR 0008 review fired on a diff of two files, not eleven. Opus answered `continue` ($0.03) and the step committed with no extra coder turn.
- S2 was the first *natural* live escalation SCIO has ever produced: the plan's `test_filter` named a test that did not exist, verify failed on "no tests collected", the coder had reported low confidence and had touched files outside the step's list, so `low_confidence, plan_drift` routed to `advise`. Opus answered `continue` with guidance ($0.05); the next coder turn made verify pass and the record resolved. Both ADR 0005 paths (review and failure) now have a live data point in one run.
- The fake coder's first version of the rename was rejected edit by edit: `edit_file` lints each edit and reverts one that leaves a name undefined, so an import line and its call site cannot change in two steps (I-057). A real coder would hit the same wall if it edited in halves.

Lessons:
- Read the entry condition, not the signal list. A trigger that exists in `detect` is only reachable from the node that calls `decide`; the eval premise failed on the call site, not the signal.
- The impact detector fires on *what* changed before *how much*: a removed public symbol flags a two-file diff. Tasks that want the file-count trigger must keep the rename and the migration in one step, or accept that the review lands on the first step.
- Natural escalations come from the harness's seams (a planned filter that matches nothing, a worker reporting low confidence), not from hard code. Fixing those seams will make the advisor rarer again; keep ADR 0008 so it still has a deterministic path.
- Rename edits are atomic per file. Scripted or live, change the import and every use in one `edit_file` call.

Actions: ADR 0008; `review_decision`, verify/advise routing, `advise -> git_checkpoint` edge, regenerated Mermaid; t010 fixture, hidden tests, fake script with whole-file caller edits; `test_passing_high_impact_diff_is_reviewed_once`, `test_t1_passing_high_impact_diff_gets_one_advisor_review`; ledger I-056, I-057; ADR 0006 live-data note updated. Open: a test_filter the planner invents should be rejected by the plan normaliser (it names a test that does not exist yet and the step does not add); candidate ledger entry once reproduced on the Fake.

## M6: Foundations for Product Mode (2026-10-12, issue #39, ADR 0009)

### Lessons applied
- Pre-flight 12 (apply a new indirection at every call site in the same change): a read-only research pass produced the full inventory of Python-specific literals before any code was written: two command builders, one lint builder, one script runner, five prompt strings, two file-count helpers, the signature parser, the drift and impact file rules, two cache lists, and the settings fields nobody read. Every item became an edit or a test.
- I-050 and I-053 (a hint is not a pin): `scio run --tier` now pins the route with the same `tier_pin` the evals use, and a budget that was only a default follows the tier the classifier picks.
- Pre-flight 6 and 10: all edits went through the file tools; every check chain started with `set -o pipefail`.
- The live-trial rule: one kept t001 trial ran on the refactored code before claiming "no behaviour change": pass, $0.11, 1 iteration, 4 tool calls, the same shape and cost as the nightly rows before M6.

### What succeeded
- The `python` profile reproduces the pre-M6 commands byte for byte, and `test_toolchain.py` says so with the old literals copied into the test, not re-derived. The regression suite passed at pass^5 = 1.00 with no fixture or script change, which is the milestone's definition of "no behaviour change".
- The boundary is mechanical: `test_no_stack_specific_command_outside_the_toolchain_package` scans `scio/` for command tokens and cache literals outside `scio/toolchain/`. It passed on the first run after the extraction, which means the inventory had been complete.
- The I-054 sweep found four more declared-but-unread settings (I-059). Grepping every field of a settings model for a reader is cheap and should be a standing check.

### What failed
- The first live t001 attempt failed before reaching the model: the `ant auth login` profile's refresh token had died (`invalid_grant`, I-060) after serving ten live trials earlier the same day. Nothing in SCIO can refresh the profile; the owner logged in again and the retry passed. Budget the re-login into any day with more than a handful of live runs.
- A `Protocol` with plain attribute annotations does not accept a frozen dataclass (mypy: "expected settable variable, got read-only attribute"). Protocol members that profiles implement as dataclass fields must be declared as read-only properties.
- `projections.py` imports from `assembler.py`, so the profile-derived prompt constants had to live in the assembler, the lower module, not where they were first written (a circular import caught at the first run).

### Actions
- `scio/toolchain/` (`base.py`, `python.py`, registry), `configs/profiles/python.yaml`, ADR 0009; `Settings.toolchain`; `RunContext.toolchain`, `make_sandbox`, `apply_tier_budgets`; `route` resizes a default budget; `scio run --tier` pins.
- `tests/unit/test_toolchain.py` (profile equality with the old literals, exit-code semantics, file rules, the boundary scan); `tests/unit/test_m6_foundations.py` (backend selection, effects path, live-test gate, default budget follows the routed tier, explicit budgets untouched).
- PROMPT.md non-goal narrowed; ARCHITECTURE F-8 superseded; README ADR list; CHANGELOG; ledger I-059, I-060.

### Carried into M7
- Stack detection for existing repositories and the `node-ts` profile are M10; until then `SCIO_TOOLCHAIN=python` is the only accepted value.
- The sandbox still carries SCIO's own Python runtime facts (`python` argv rewrite, `PYTHONUTF8`, `PIP_NO_INDEX`); M9's two-phase network design decides whether those move into the profile or stay with the sandbox.

## M7: Greenfield bootstrap (2026-10-12, issue #42, ADR 0010)

### Lessons applied
- A read-only research pass before any code (as in M6) listed the mechanics to reuse: `interrupt` payload shapes and `Command(resume=...)`, the structured-output and role pattern, static prompt templates, the Fake responder's schema markers, the serializer allow-list, `run_effect` with world probes, and the CLI's dispatch. Every one was reused rather than reinvented; the product graph is 400 lines because of it.
- I-039 and I-046: the planner's draft goes through a normaliser (`scio/product/spec.py`) before a human sees it, and the notes about what was changed are in the ledger and the approval payload.
- The live-trial rule: one kept `scio new` run from an empty directory, read before claiming done.

### What succeeded
- The whole path ran on the Fake on the first attempt after lint and types were clean: interview pause, answers, spec pause, approval, bootstrap, green local CI, crash-style resume with no duplicate commit, three rejections halting, the CLI contract. Twelve tests, then a thirteenth for the CLI.
- The live run from an empty directory: Opus asked four questions that were all about things the prompt left open (who installs it, the day-one command sequence, runtime and storage, what the export is for), wrote a ten-feature spec whose criteria are black-box assertions (exact outputs, exit codes, file paths), the scaffold's lint and tests were green, and the bootstrap commit carried the marker. Cost $0.15: interviewer $0.025, planner $0.127, no other roles.
- Ground truth before commit: the profile's lint and test commands ran on the scaffold and a failure would have halted with `bootstrap_failed`; no model verdict is involved in the bootstrap.

### What failed
- The first live run never reached a model: `scio new --prompt ... --thread new-live1` resumed a thread that did not exist (I-061). The Fake tests drove the library, not the CLI, so the contract was untested; the CLI test now drives all three calls.
- The first lint and type run after writing the graph found eight findings each: an unused import, three list concatenations, four long lines in the licence text, a `Protocol`-typed LangGraph call the task graph avoids by typing its compiled graph as `Any`, and `BudgetExceeded` imported from the wrong module. All caught by the chain in the same step, none reached a commit.
- The planner's `P1` ("`todo --version` prints `todo 0.1.0`", a console command named `todo`) is not what the scaffold builds (`python -m tagtodo --version` prints `0.1.0`), yet `progress.json` marks P1 done because the scaffold's own smoke test passed. The skeleton the planner describes and the skeleton the scaffold ships are two different things.

### Actions
- `scio/product/` (graph, spec normaliser and renderer, scaffolds), `ProductState` and the two output schemas in `scio/state/models.py`, roles `interviewer` and `product_planner` with templates, `scio new` in the CLI, Fake markers `interview_questions` and `product_name`.
- `tests/graph/test_product_new_fake.py` (end to end, resume, rejection cap, normaliser invariants, CLI contract); ADR 0010; README usage; CHANGELOG; ledger I-061.

### Carried into M8
- P1 ownership: either the scaffold must satisfy the planner's P1 criteria (install a console script named after the product and print `<name> <version>`), or P1 must be run as the first feature task against the scaffold instead of being marked done. Decide in M8 before the milestone loop trusts `progress.json`.
- `scio report` has no product-thread view; add one when the milestone loop produces escalations and costs worth reporting.
- Publishing (`--gh`) is implemented behind a confirmation interrupt but has no Fake coverage: `gh repo create` needs an injected runner like `GitHubClient` has.

### Carried forward (post-0.1.0)
- I-019 (placeholder key): re-run the regression suite, the A/B configs and `evals/tune.py` live when a key exists; revisit ADR 0006 decision 1 with real resolution rates. Update 2026-10-12: the suite and configs run nightly; the sweep still waits, because no live run has escalated (31 task rows, zero advisor calls) and a sweep over runs that never escalate measures nothing.
- I-016: Docker and E2B live checks on a machine that has them.
- I-008, I-009: cosmetic pdoc warning; branch protection needs GitHub Pro or a public repository.
- Open question 3 (patch sampling): the sampling step is wired but off; measure before enabling.

## M8: Decompose + milestone loop (2026-10-12, issue #44, ADR 0011)

### Lessons applied
- Pre-flight 1: the whole file and the open ledger entries were read first. Two entries (I-053, I-054) were still `open` although M6 had fixed them; they were completed before any M8 code (an entry is finished when the fix lands, not when the retrospective mentions it).
- Pre-flight 8: a six-line probe script confirmed, before the design was fixed, that a graph invoked inside another graph's node with its own `thread_id` gets an independent checkpoint thread (namespace `""`, resumable from a fresh graph object). The nested-subgraph design rests on that measurement, not on the LangGraph docs.
- I-046 (the model reports, the plan decides): the milestone planner proposes a grouping and tiers; `normalise_milestone` enforces PRODUCT.md order, a consecutive prefix from the first open feature and the cap, and the orchestrator alone picks the next feature.
- I-050 and I-053 (a hint is not a pin): every feature's `RunContext` carries `tier_pin`, so the router is never consulted inside a product build.
- ADR 0003: every side effect in the loop (milestone branch, issue files and their commit, GitHub milestone, issues, the fast-forward after a feature, push, PR, ready, merge or pull, the progress commit) is a `run_effect` with a world probe, and the crash tests kill the process at the second feature's gate and after the PR.
- The M7 carry-over (P1 ownership) was decided first, as option (b): every feature starts `todo` and P1 is a feature run. The M7 test that asserted P1 `done` was changed with the ADR, not worked around.

### What succeeded
- The whole loop ran on the Fake at the first attempt once lint and types were clean, except for one real harness defect it exposed (below): planner, issues, P1 at T0, P2 at T1, fast-forwards, integration checks, gate, PR file, merge, progress commit, second milestone, done, and the product's own CI green on `main`.
- Sharing the orchestrator's open checkpointer with each feature subgraph (`run_graph(..., saver=...)`) avoided a second SQLite connection inside a running node; the deterministic thread id per attempt made crash resume a probe (`thread_started`) instead of state bookkeeping.
- `Harness.child` gave each feature its own budget guard and cost ledger while keeping one provider registry (and so one SDK client per process, I-044); the product's `FeatureRun` records cost, iterations and escalations straight from the subgraph's final `SCIOState`, and the typed-handoff test compares the two.
- The GitHub path was tested end to end without GitHub: a canned `gh` runner for milestones, issues, PR and merge state, plus a bare local `origin` for the real push and pull.

### What failed
- The first Fake build stalled on P1: its only edit creates a new test file, and verify's "something changed" check read `git diff --stat`, which ignores untracked files (I-063). Ten eval tasks and every live run had edited an existing file, so the hole was invisible. The P1 script is now a new-files-only task in CI.
- `uv.lock` had been stale since 0.3.0 (I-062): the release chore never relocks. `test_version.py` now checks the lock.
- A crash test counted commits on the feature branch and forgot that the branch includes the milestone's earlier commits; the assertion, not the code, was wrong (`milestone/1..agent/...` ranges fix it).
- The first live `scio new` after M8 approved a different `PRODUCT.md` from the one shown ("tagtodo, 8 features" on screen, "todo_cli, 7 features" on disk) and paid the interviewer and the planner twice (I-065): both M7 nodes made their model call and interrupted in the same node, and a node re-runs from its start on resume. The M8 loop had been written with its stops in separate nodes (`gate_decision`, `merge_wait`) without going back to M7. The scripted Fake returns the same draft every call, so no test could see it; the guard test now varies the draft per call and counts calls. Pre-flight item 15.

### Live milestone (2026-10-12, `sonnet_opus_advisor`)
`scio new` from an empty directory (interview, approval, bootstrap: $0.13 across four Opus calls, two of them the I-065 duplicates) then `scio build` for one milestone on the resulting `todo_cli` product (seven features in PRODUCT.md). The planner grouped P1 to P4 as M1 "Walking skeleton, storage, add and list" with P1 and P4 at T0, P2 and P3 at T1.

| feature | tier | result | iterations | tool calls | cost | escalations | what happened |
|---|---|---|---:|---:|---:|---|---|
| P1 walking skeleton (console script `todo --version`) | T0 | skipped by human | 1 | 22 | $0.21 | 0 | the coder wrote the entry point, `[project.scripts]` and a subprocess smoke test, then asked a design question: the sandbox has no install step, so `todo` is not on PATH (M9 scope) |
| P2 JSON file storage | T1 | pass | 4 | 44 | $0.77 | 1 resolved (`plan_drift, high_impact`, advisor `change_approach`) | verify failed on a test P1 had left in the tree (I-067); the advisor named the drift, the guided turn fixed it, two more steps passed first time |
| P3 `todo add` with tags | T1 | pass | 2 | 23 | $0.52 | 0 | plan, localize, one coder turn per step |
| P4 `todo list` | T0 | pass | 1 | 12 | $0.23 | 0 | one coder turn |
| orchestrator | | | | | $0.05 | | milestone planner $0.023, milestone gate $0.030 |

Integration checks on `milestone/1`: lint clean, 20 tests passed. The milestone gate returned `escalate_human` on its own ("P1 has no commit, so no feature run covers its criteria") before the orchestrator's override could apply; `proceed` opened the PR file, `merge and stop` merged `milestone/1` into `main` with the `[scio:merge:M1]` marker and recorded M1 in `progress.json` ($1.73 features, $1.78 total). Human stops: four (P1's decision, the gate, the merge, and the approval of PRODUCT.md).

Lessons:
- The planner's P1 criteria need an installed console script, and the M8 sandbox cannot install anything (`PIP_NO_INDEX`, no `install_deps` until M9). The coder diagnosed it exactly and asked instead of faking a pass; the orchestrator turned that into one decision. Until M9, the walking-skeleton prompt should ask for `python -m <pkg>` criteria only, or the planner's P1 should be rewritten by the normaliser.
- A failed attempt leaves a dirty tree, and the next feature inherits it (I-067). The orchestrator now resets the milestone branch before every fresh attempt.
- Every stop that re-runs a model call pays twice and may act on a draft nobody saw (I-065). Found only because the live planner is non-deterministic; the Fake now varies drafts in the guard test.
- After the merge the loop went straight into planning M2 with no stop and no cap; `merge and stop` now exists and threads are named per starting milestone.
- `scio report` on a feature thread inside a build crashed (I-066): the thread kind must come from the checkpoint, not the name.
- Cost shape: a T1 feature on a small product costs $0.5 to $0.8, dominated by coder turns (59% of P2, 53% of P3); the explorer and documenter are the next largest. The orchestrator's own calls are 3% of the milestone.

### Actions
- `scio/product/build.py` (the loop), `scio/product/progress.py` (PRODUCT.md parser, `ProgressFile`, `normalise_milestone`), `scio/product/report.py`, `evals/product_scripts.py` (three scripted features), roles `milestone_planner` and `milestone_gate`, `Harness.child`, `GitHubClient.ensure_milestone` / `ensure_issue` / `find_issue` / `pr_merged`, `run_graph(..., saver=)` and `thread_started`, `scio build` / `scio status` / `scio report` for build threads, registered `product_new` and `product_build` diagrams.
- `tests/graph/test_product_build_fake.py` (ten tests: the two-milestone loop, crash between features, GitHub mode crash after the PR against a bare origin, gate override, integration ground truth, escalations across features, feature decisions, gate escalation, the CLI), `tests/unit/test_product_progress.py`, GitHub client idempotency tests, `test_diff_stat_counts_untracked_files_as_changes`, the lock-version test.
- ADR 0011; README; PRODUCT_MODE status; ARCHITECTURE product graphs section and tree; CHANGELOG; ledger I-062 to I-067, I-053 and I-054 completed; `answers` and `approval` nodes (I-065); `merge and stop`, per-milestone build threads; the working-tree reset before a fresh attempt (I-067); report thread detection by checkpoint shape (I-066).

### Carried into M9
- I-064 is closed by the live milestone above. Before M9: make the walking-skeleton criteria installable without a package install, or give the normaliser a rule for P1 (the planner wrote a console-script P1 twice in two live runs).
- `integrate` runs tests and lint only; the build, smoke against a running app, dependency audit and the coverage floor arrive with M9's runtime commands (`install_deps`, `run_app`) and the Docker default.
- A pre-flight credential probe for live product commands (one cheap call before the interview) would turn I-060/I-064 into a one-line message instead of a traceback.
- Repositories bootstrapped by 0.5.0 carry the old `progress.json` shape (P1 `done`, one milestone); `ProgressFile.sync_features` adds missing records but does not reset P1. Re-bootstrap or edit by hand until a migration is worth writing.

## Claude Code backend (2026-10-12, issue #47, ADR 0012)

### Why
After the M8 live milestone the owner said they had been billed for API usage and want only their Max subscription to pay. Anthropic's terms allow a subscription login only inside the unmodified Claude Code binary; the Max plan's included API credits are the other sanctioned route (and remain the fallback: link the Console org on claude.ai, no card). The backend makes SCIO run its model work through `claude -p`.

### Lessons applied
- Pre-flight 8: every flag the backend relies on was read from `claude --help` and the headless docs, then exercised live (a restricted `--json-schema` call on the Max login) before any code: `--bare` would have silently switched to API-key auth, and print mode refuses to nest under `CLAUDECODE=1`.
- The `GitHubClient` pattern (an injectable runner, every argv recorded) made the whole backend testable without the binary or a login: the canned runner in the graph test performs the scripted edits in the sandbox, so SCIO's real verify grades what "Claude Code" wrote.
- `scripts/check_docs.py` caught model ids written into the new tests; they now come from `models.yaml` through the role table.

### What succeeded
- Two seams were enough: a `ModelProvider` for tool-less roles and a worker with `AgentLoop`'s surface behind `make_worker`; the four node call sites changed one line each and every existing test passed unchanged on the `aci` backend.
- `stream-json` rendered into SCIO's transcript shape keeps `scio report`'s tool signals working for Claude Code turns; `Harness.account` keeps budgets, caps and the cost ledger honest with Claude Code's own usage numbers.

### What failed
- The first live trial script passed an `EvalTask` where the solver wants `to_task_like(task)`; a harness detail, no spend.
- The first `scio new` through the backend died on its first call: print mode returned `is_error` with no message, and the provider called every unexplained error fatal (I-068). The harness already had a retry policy; the provider just never raised the retryable class. Transient process failures are now retried and the subtype is carried in the message.
- The first `scio build` through the backend died in P1's documenter turn: the T0 cap of $0.50, written for API prices, was handed to `--max-budget-usd` as the remaining $0.29 and Claude Code stopped the turn; the backend turned that into a traceback instead of SCIO's `budget` exit (I-069). Two fixes: tier caps scale by a measured factor (4.0) on this backend, and the binary's budget stop maps to `BudgetExceeded`, which every node already handles. The eval solver scales task budgets the same way.
- P4's documenter wrote a 1,200-character summary against SCIO's 800-character cap, the re-ask came back long again, and the build died (I-070): the schema handed to `--json-schema` carries no `maxLength`, so the binary cannot enforce prose caps. The worker now trims over-long string fields and validates again before any re-ask; structural errors still re-ask once.
- A lesson about guards: a cap expressed in dollars is only as portable as the price list behind it. When a backend changes what a dollar measures, scale the guard with data from one trial before running anything long.

### Live trial (2026-10-12, Max login, no Console API usage)
t001 through the T0 graph with both seams on (`SCIO_PROVIDER_OVERRIDE=claude_code`, `SCIO_WORKER_BACKEND=claude_code`), run from inside a Claude Code session with `CLAUDECODE` stripped:

| role | `claude -p` runs | output tokens | cache read | cache write | API-price estimate |
|---|---:|---:|---:|---:|---:|
| router (haiku) | 1 | 152 | 0 | 10,747 | $0.001 |
| coder (sonnet) | 2 (session + summary) | 2,602 | 205,212 | 60,010 | $0.217 |
| documenter (sonnet) | 4 (two rounds) | 6,977 | 405,516 | 52,892 | $0.283 |
| gate (opus) | 1 | 354 | 0 | 11,697 | $0.066 |
| total | 8 | | | | $0.57 estimated, $0.00 billed |

Result: pass. The coder's session listed the repository with a read-only Bash call, read the module and the tests, wrote `slugify` and `tests/test_slugify.py`, ran pytest; SCIO's verify passed on the first iteration, `git_checkpoint` committed `feat:`, the documenter updated the CHANGELOG, the gate passed, and the hidden tests pass on the kept tree (9 tests). Nothing was sent to the Console organisation.

Lessons:
- The same task costs about four times its API estimate when run through Claude Code ($0.57 versus $0.11 to $0.15 for t001 on the API), because every `claude -p` process carries Claude Code's own system prompt and tool set as cache writes and reads (60k cache-write tokens on the coder's first turn). On a subscription this is usage-window consumption, not dollars; the estimate is still the right number to cap turns with.
- The documenter's two rounds are four processes; `--resume` keeps the second round's context but each round pays the session start. A single-round documenter would halve its cost on this backend.
- Print mode's read-only command set (`ls`, `cat`) runs without an `--allowedTools` entry, so the coder explores with Bash even though only the test and lint commands were allowed; `acceptEdits` plus `--permission-prompts none` never blocked the run.

### Live milestone through the backend (2026-10-12, Max login, no Console API usage)
`scio new` (interviewer and product planner through `claude -p`, $0.04 estimate; the approval applied to the draft shown, the I-065 fix holding live) bootstrapped `todo_tags` (8 features, P1 needing only `python -m`), then `scio build` ran M1 "Walking skeleton with storage, add and list" with every role through Claude Code:

| feature | tier | result | runs (`claude -p`) | estimate | what happened |
|---|---|---|---:|---:|---|
| P1 walking skeleton | T0 | pass | 7 | $0.50 | first attempt died at the documenter's `--max-budget-usd` (I-069); resumed after the budget fix, passed |
| P2 JSON storage | T1 | pass | 18 | $1.93 | plan, localize, a coder that needed four verify iterations, reviewer, documenter, gate; no escalation |
| P3 `add` with tags | T0 | pass | 7 | $0.62 | one coder turn |
| P4 `list` | T0 | pass | 9 | $1.05 | documenter summary over 800 characters crashed the build once (I-070); resumed after the trim fix, passed |
| orchestrator | | | 3 | $0.16 | planner (twice: the first attempt's call plus the resume), gate pass |
| total | | 4 of 4 | 44 | $4.26 estimated, $0.00 billed | integration green (25 tests, lint clean), milestone gate pass, merged locally with `merge and stop`; the product's own suite and lint pass on `main` |

Lessons:
- Three defects surfaced only live, each in the seam between SCIO's guards and the binary's behaviour: an unexplained transient error (I-068), a dollar cap sized for API prices (I-069), and a prose cap the binary cannot see (I-070). All three were fixed between resumes and the same build thread carried on from its checkpoint each time, which is what the crash-safety work in M8 was for.
- Cost shape on this backend: the T1 feature (P2) cost four times the T0 ones, dominated by nine coder runs; each `claude -p` process re-sends Claude Code's prompt, so iterations are the lever, as on the API, but with a higher constant. A milestone of this size is about $4 of estimate, or roughly a tenth of a Max 5x usage window by the same yardstick.
- The documenter is the most expensive role per feature after the coder (two rounds, each a session plus a summary); M9 should give it one round with the docs check folded into the turn.

## M9: Dependencies + runtime (2026-10-13 to 2026-10-26, issue #49, ADR 0013)

### Lessons applied
- The read-only inventory before any code (as in M6 to M8) listed every seam M9 touches, with lines, and became the issue body; the slices followed it in order: sandbox `delete` and the Claude Code local-only guard, the profile's quality bar, the product environment and install phase, `install_deps` and `run_app`, the `## Runtime` section and the acceptance runner, then the http Fake product.
- Pre-flight 8: the venv, pip and app commands are profile data (`runtime:`), exercised by real processes in the tests (a venv is created, a server is started and probed), not mocked.
- I-063 made the P1 scripts possible: both Fake products' first features add only new files.
- The Claude Code backend's boundary (ADR 0012) shaped the design: installs are an orchestrator step before the turn, never a worker act, so the same code path serves both backends.

### What succeeded
- The done criterion's shape runs offline: `todo_api` approves `pyyaml`, the install phase creates its environment, two features build through the task graph, `integrate` starts the server on a free port, runs the black-box acceptance tests against it, stops it, the milestone merges and the tree is clean.
- "Skipped, tool absent" as a first-class outcome keeps the quality bar honest on machines without `build` or `pip-audit` and lets a product's own CI be the place that installs them.

### What failed
- Shell-embedded patches mangled backslashes three times in one session (I-071, I-072); the rule has existed since M1. The fix is a memory rule for the assistant: edits with backslashes or quote-heavy literals go through the file tools, one site per edit.
- The first `todo_api` build stalled on a feature that was correct: two test modules shared a basename across `tests/` and `tests/acceptance/` and pytest refused to collect (I-073). The scaffold's test directories are packages now.
- The coverage check left `.coverage` in the product tree, which a later feature's `git add -A` would have committed; the profile's cache excludes carry the coverage artifacts now.
- The first push of PR #50 was red on CI and green locally (I-074): the product venv's `--system-site-packages` exposed the *base* interpreter, and only the dev machine's base Python happened to carry pytest. A test asserted that pytest imports, not where it comes from, so the accident passed. Rule: when a test depends on a tool being present, assert its origin (`__file__` under the expected path), and never rely on `--system-site-packages` under `uv run`.
- The dev machine's Application Control policy began blocking the venv's `pytest.exe` and `scio.exe` launchers (I-075); the module forms are the fixed spelling in CLAUDE.md now, and `scio/__main__.py` exists so `python -m scio` works.

### Actions
- `_module_only_skeleton` in `scio/product/spec.py` and the planner rule: a P1 criterion that needs a console script, an entry point or an install becomes the `python -m <package>` form (two live planners had asked for one).
- `scio/aci/runtime.py`, `scio/product/acceptance.py`, `Sandbox.delete` and `exec(network=)`, `ToolDef.needs_context`, `QualityCheck` and `Runtime` in the toolchain, `AppSpec` and the `## Runtime` section, the scaffold's acceptance tests and http skeleton, `evals/product_scripts_http.py`; tests `test_runtime.py`, `test_acceptance.py`, the delete and quality-bar tests, the http product loop test; ADR 0013; ledger I-071 to I-073.
- `link_host_env` and `Runtime.test_tools` (plain venv, `.pth` link on local, tools installed elsewhere), the origin assertion in `test_product_env_is_created_once_and_rewrites_python`, `test_non_local_sandbox_installs_the_test_tools_on_creation`; `scio/__main__.py` and its test; CLAUDE.md's CI-equivalent line; ledger I-074, I-075.

- Docker default (ADR 0013 decision 10): `DockerSandbox` rewritten around one persistent container on an internal network with the egress-proxy sidecar (`scio/aci/sandbox/egress_proxy.py`), `docker_product_ready`, `product_sandbox_backend` and `Settings.product_sandbox_backend`, the ledger notice in `load`, the in-sandbox branch of `run_acceptance`, `docker/sandbox.Dockerfile`, the `docker-live` CI job; tests `test_sandbox.py` (command shapes), `test_egress_proxy.py`, `test_product_sandbox_backend.py`, `tests/live/test_docker_live.py`. Written blind on a machine without Docker (I-016): the first `docker-live` run passed 7 of 9 (a host-side symlink check, I-078, and a wrong expectation about a refused CONNECT, I-079), the second passed 9 of 9 in 19 seconds. The container, the internal network, the proxy's allowlist, the in-container product environment and the in-container http app all worked on the first real run.
- The first live http milestone found two bugs before any code was written: a resume of `scio new` without `--dir` bootstrapped in a default directory (I-076), and the Claude Code worker's typed summary had one turn where Claude Code's own schema bounce needs two (I-077). Both are fixed with tests; the same build resumed from its checkpoint.

### The live http milestone (2026-10-26, Claude Code backend, nothing billed)
`scio new` interviewed and planned `bookmarks_api` (an http.server service persisting to YAML through `pyyaml`, `## Runtime` with `serve --port {port}` and `/health`, 7 features, $0.12 estimated). `scio build` ran M1 "Walking skeleton with bookmark create and list": P1 to P3 at T1, P4 at T0, every feature passed its own verify, one advisor escalation (P1 changed `pyproject.toml`, verdict continue), 63 tool calls and 23 minutes for P1, about 8 minutes for each of the others. Integration on the merged branch: lint clean, 28 tests passed, coverage passed, build and audit skipped (tools absent), `[acceptance] passed (http http://127.0.0.1:63721): 2 passed`, that is the black-box tests ran against the server SCIO started from the product environment. The gate passed, `merge and stop` merged `milestone/1` into `main` and recorded M1 in `progress.json`. Cost: $5.38 estimated over 41 Claude Code runs, $0.00 billed (Max subscription). The M9 done criterion holds on the Fake and live.
- What it found before any code was written: I-076 (resume without `--dir`) and I-077 (one turn for a schema answer). Both fixed in the same session; the build resumed from its checkpoint without a duplicated commit.
- Honest limit: `pyyaml` was "already satisfied" through the `.pth` link to SCIO's environment, so the live run did not download anything; the Docker CI test (`test_product_environment_lives_inside_the_container`) is where a real install through the allowlist proxy is exercised.

### Still open after M9
- `run_app` inside a container; a lockfile for the install phase; the Docker default has run only in CI.

## M10: Second stack (2026-10-26, issue #51, ADR 0014, PR #52)

### Lessons applied
- Inventory first (M6, pre-flight 12): a read-only sweep before any code listed every Python assumption M7 to M9 had re-introduced outside the profile (the scaffold, the normaliser's `python -m` literals, the venv runtime, the acceptance runner's directory and health probe, `run_tests` and `edit_file` never receiving the context's toolchain, the Claude Code allow-rules, the planner prompt, every Fake script); each became an edit in slices 3 to 5 and the list is the issue body.
- The graph names what, a profile says how (ADR 0009): every new need became a protocol member (`acceptance_dir`, `skeleton_command`, `command_words`, `app_target`, `syntax_check_argv`, `health_probe_argv`, `detect`) and the yaml says how; no `if stack ==` was written.
- Tests never depend on what the machine has (I-074, I-080): `tests/node_support.py` skips the Node tests where Node 22 is absent; CI's `checks` job installs it, so the tests count there.
- Measure before designing (pre-flight 8): the TAP shape, "no test files" being exit 0 with `# tests 0`, the name filter, absolute globs, `node --check` on `.ts`, `npm` being a `.cmd` shim and `process.features.typescript == "strip"` were each probed on this machine before the yaml was written.
- One live trial after the Fake (M5): the live `node-ts` milestone runs on the Claude Code backend after the Fake both-stacks build passed.

### What succeeded
- The python profile moved onto the generic loader with its tests unchanged (`test_toolchain.py` still compares the old literals), which is the definition of "no behaviour change" for the refactor.
- The TypeScript walking skeleton runs, tests, lints (eslint absent is "not the agent's problem"), serves its health path and passes black-box acceptance with no download, on the first run of the scaffold test.
- `test_same_spec_builds_in_both_stacks`: the `todo_api` spec bootstraps and builds M1 in Python and in TypeScript through the unchanged milestone loop: install phase, two features through the task graph, the started server, the black-box run, the quality bar (`[typecheck] skipped: tool absent`), merge, a clean tree, the product's own CI commands green.

### What failed
- `run_tests` grew a context parameter in the position where verify passed the toolchain, and the python default answered for the TypeScript product: pytest found no tests in a repository with none (I-082). The unit tests were green because the python build was unaffected; the both-stacks Fake build found it in one run. A default that hides a wrong call is the hazard pre-flight 12 names.
- `node --check` accepted a TypeScript parameter property that the strip-only loader refused, so the edit gate let a file through that broke every import (I-083); the gate now uses the loader's own parser (`module.stripTypeScriptTypes`).
- `subprocess.run(["npm", ...])` cannot spawn npm's `.cmd` shim on Windows (I-081); the local sandbox resolves bare commands through `shutil.which` there.
- The first CI run of the branch failed the three Node tests with "Cannot allocate Wasm memory": the local sandbox's POSIX `RLIMIT_AS` (2 GB of *virtual* address space) killed V8's type stripper, a limit the Windows dev machine never applied (I-084). Third "green here, red there" of the day after I-074 and I-080; the dev machine and the runner differ in packages, environment variables and now resource limits, and CI is the only place all three are the runner's.

### The live node-ts milestone (2026-10-26, Claude Code backend, nothing billed)
`scio new` with a TypeScript brief: the planner (prompt v2, the stack menu rendered from the registry) chose `node-ts`, `src/main.ts` as the entry, `zod` as the one approved dependency, six features with black-box criteria; the bootstrap landed in the directory passed on the first call (the I-076 fix holding live) with the scaffold's lint and `node --test` green. `scio build` ran M1 "Walking skeleton with note create and list": P1 and P2 at T1, P3 at T0, every feature passed its own verify, one advisor escalation (P1 changed `package.json`). The install phase ran `npm install` through the local sandbox on Windows (the `.cmd` resolution of I-081) and, because the scaffold declares the quality tools as devDependencies, installed eslint, typescript and the type definitions too, so the quality bar ran for real: `[lint] clean`, 13 tests passed (1 skipped: the acceptance test awaiting `APP_URL`), `[typecheck] passed`, `[build] passed`, `[audit] passed: found 0 vulnerabilities`, `[acceptance] passed (http ...)`. The gate passed and `merge and stop` merged `milestone/1`. Cost: $4.61 estimated over 36 Claude Code runs, $0.00 billed. The M10 done criterion holds on the Fake and live.
- Found: the install phase leaves `package.json` modified (`npm install --save` rewrites the dependency range) and `package-lock.json` untracked in the product tree after the merge (I-085, open): the lockfile PRODUCT_MODE component 4 asks for exists now but nothing commits it, and the next feature's `git add -A` would.

### Still open after M10
- I-085: commit the install phase's manifest and lockfile changes as their own `[scio:install]` commit on the milestone branch (or install without `--save`).
- Docker for Node products (the sandbox image is Python); `run_app` inside a container; the `test_` prefix in `escalation.py`'s drift rule.

### Actions
- `scio/toolchain/profile.py` (`ProfileToolchain`, `StackParsers`, `load_profile`), `node_ts.py`, `configs/profiles/node-ts.yaml`, the registry with `available_toolchains` and `detect_toolchain`; `Runtime` generalised; `resolve_command`; `ensure_product_env` on `env_marker`; the stack-neutral normaliser, acceptance runner, `run_app`, tool descriptions, allow-rules, planner prompt v2; `node_ts_scaffold` and the `scaffold_for` registry; `resolve_toolchain`, `adopt_toolchain` and the four call sites; `evals/product_scripts_node.py` and `ScriptedResponder(stack=)`; `LocalSandbox` without `RLIMIT_AS` (I-084); `check_readme_status` (the landing page current at every push).
- Tests: `test_toolchain_node_ts.py`, `test_scaffold_node_ts.py`, the node cases in `test_runtime.py` and `test_sandbox.py`, the normaliser test in both stacks, `test_same_spec_builds_in_both_stacks`, `tests/node_support.py`; CI `setup-node`.
- ADR 0014; PRODUCT_MODE status; README status, table and roadmap; CHANGELOG; ledger I-081 to I-085.

## M11: Product-ready + evals (2026-10-26 to 2026-10-27, issue #54, ADR 0015, PR #56)

### Lessons applied
- Inventory first (M6 to M10): a read-only sweep listed the quality-bar gaps, the loop's end, the install path and the eval runner before any code; it became the issue body and the nine slices.
- Measure before designing (pre-flight 8): npm's save and lockfile behaviour, Node 22's coverage flags and its coverage-ignore comments, the scaffold's real coverage (78.57% and 33.33% once tests stopped counting) and a headless Chromium launch were each probed before the code that depends on them.
- A node that interrupts does nothing else (I-065): `release_decision` only asks; the tag and the Release are effects in `release`.
- The failure must be visible to the worker (t008, t009): coverage and node's `typecheck` run in each feature's verify, not only in `integrate` (I-091).
- Green here, red there (I-074, I-080, I-084): the Docker image tests were written for CI's daemon and passed there first time; the browser tests run in CI's `checks` job, which installs Chromium.
- A hint is not a pin (I-050): each product eval pins its stack and interface in a fixed `PRODUCT.md`.
- One live trial before scaling (M5), adjusted by the owner on 2026-10-27: on the Claude Code backend nothing is billed, so the live evals ran without a stop between them.

### What succeeded
- The release gate caught two real defects that every per-feature check had passed: a flaky scripted server (I-096) on the Fake, and nothing on live p001, whose release was clean. Running the whole bar once more on `main` is cheap and earned its place on its first day.
- The honest coverage floor plus the verify-time check works as designed on the Fake: P2 at 93% against a floor of 95 failed its own verify (`quality`), went to the debugger, gained the error-path tests and passed in its second iteration; without the verify step the milestone halts at `integrate`.
- On the final commit all three product evals pass live, in parallel: a CLI todo app with tags and JSON export (p001, 3 features), a REST todo API (p002, 2 features) and a todo web app graded in a real browser (p003, 2 features), each from a fixed `PRODUCT.md` to a released v0.1.0 whose hidden black-box tests pass, with no intervention beyond the designed merge and release stops: $11.63 estimated in all, 21 to 31 minutes each, nothing billed.
- Writing guards before trusting green found more than the guards themselves: the offline I-085 test found I-088, the I-090 sweep found the bootstrap probe's regex bug, the I-085 sweep found the uncommitted progress sync.

### The live product evals (2026-10-27, Claude Code backend, the owner's Max login, nothing billed)
Every attempt, one trial each; dollars are Claude Code's API-price estimates. First attempts passed 1 of 3, and each failure was a defect in SCIO, not in the model's work. On the final commit all three pass.

| task | commit | result | features (tier, iterations, cost) | release | hidden | estimate | time | stops / interventions | found |
|---|---|---|---|---|---|---:|---:|---|---|
| p001 CLI todo | `6f7474a` | pass | P1 T0 2 $0.88; P2 T1 3 $1.47; P3 T1 2 $1.44 (1 advisor escalation, resolved) | v0.1.0 | 5 passed | $3.96 | 24.5 min | 3 / 0 | P1 used 353,267 tokens of a 400,000 T0 cap (I-099 waiting to happen) |
| p002 REST API | `6f7474a` | fail | P1 T0 1, `escalate_human` | none | not run | $0.80 | 6.8 min | 1 / 1 | I-097 |
| p002 REST API | `5eeac8d` | pass | P1 T0 2 $0.82; P2 T1 3 $1.56 | v0.1.0 | 5 passed | $2.43 | 18.7 min | 2 / 0 | P1's first verify refused the all-skipped proof with the new hint, the next turn passed: I-097 confirmed live |
| p003 web app | `5eeac8d` | crash | P1 passed; P2 ran out at its PR body | none | not run | not recorded | 22.9 min | 0 / 0 | I-098, I-099 |
| p003 web app | `94b8849` | pass | P1 T1 3 $1.75; P2 T1 2 $1.71 (1 escalation) | v0.1.0 | 4 passed in Chromium | $3.51 | 24.4 min | 2 / 0 | both features planned at T1, so this run did not exercise I-099 (the measured ratio and its unit test are the evidence) |
| p001 CLI todo | `94b8849` | pass | P1 T0 1 $0.56; P2 T1 3 $1.72; P3 T1 2 $1.44 | v0.1.0 | 5 passed | $3.84 | 25.6 min | 3 / 0 | none |
| p002 REST API | `94b8849` | crash | P1's PR body: Claude Code's schema check refused every answer within three turns | none | not run | not recorded | 5.6 min | 0 / 0 | I-100 |
| p001 CLI todo | `dda626f` | pass | P1 T0 $0.82; P2 T1 $1.26; P3 T1 $2.19 (one milestone) | v0.1.0 | 5 passed | $4.41 | 31.0 min | 2 / 0 | none |
| p002 REST API | `dda626f` | pass | P1 T0 $0.76; P2 T1 $1.90 | v0.1.0 | 5 passed | $2.79 | 21.0 min | 2 / 0 | none |
| p003 web app | `dda626f` | pass | P1 T1 $2.03; P2 T1 $2.27 | v0.1.0 | 4 passed in Chromium | $4.43 | 30.6 min | 2 / 0 | none |

On the final commit `dda626f` all three pass, one trial each, run in parallel: $11.63 estimated in all, nothing billed, no intervention beyond the designed merge and release stops. Across the day: 7 passes in 10 attempts; each of the other 3 (one failure, two crashes) found SCIO defects (I-097; I-098 with I-099; I-100), fixed with a guard before the next attempt.

### What failed
- Live p002's first run halted at its first feature (I-097): the coder rightly found the health endpoint already built and "proved" it with black-box tests that skip outside `integrate`; verify accepted an all-skipped run, and only the model gate's `replan` noticed. The M3 lesson covered "no tests collected" but not "all skipped". A pass that ran nothing is not a pass, in every reader of a test run.
- Release cadence slipped for two weeks (I-086) and I acted on a reported merge without checking GitHub (I-087); both are now checks or rules, not intentions.
- Three of my own mistakes reached a check before a test did: stack data in product code (I-093, caught by the M6 boundary test), a CI timeout nobody had measured (I-094, cancelled a green run), the trial spend that missed every feature's child harness (I-095, caught on the first Fake run before any live run).
- Git's auto-merge silently moved branch entries into a released changelog section (I-089); a text merge does not know which section a line belongs to.
- Live p003's first run ended in a traceback (I-098): one model call in the documenter (the PR body) and the router's call were outside any `BudgetExceeded` guard. Every other node had one; a convention nobody checks is a convention that drifts, so the AST scan now checks it.
- Behind it, a runaway guard calibrated on the wrong quantity (I-099): I-069 measured the dollar ratio of the Claude Code backend and scaled token caps by the same number, but that backend's tokens are mostly cheap cache traffic. Two live product runs gave the same ratio (about 380,000 billable tokens per estimated dollar), which is what the token factor is now set from. A guard needs its own measurement for each quantity it caps.

### Actions
- `commit_install`, `commit_progress_sync` (I-085); `_npm_dependency` and `normalise_package` for non-registry specs (I-088); `check_release_cadence` (I-086), `check_released_section_frozen` (I-089); `find_commit` in the bootstrap probe and the fixed-string scan test (I-090).
- `Settings.coverage_floor`, `QualityCheck.command` and `verify`, `RunContext.quality_values`, the `quality` failure kind; both scaffolds test in-process and mark the serve lifecycle; `test_scaffold_coverage.py`; the coverage-floor failure-path test (I-091).
- `scio/product/release_checks.py` (secret scan, release files, README run block; I-092), `release_image` in both profiles (I-093), Dockerfiles in both scaffolds, the four `docker-live` image tests; CI `checks` budget 25 minutes with `--durations` (I-094).
- The release gate (`release_check`, `release_decision`, `release`, `ReleaseRecord`, `GitHubClient.find_release` / `ensure_release`) and its Fake tests.
- `scio new --spec`; the product suite (`grader: product`, `evals/product_solver.py`, p001 to p003, `--task`), `TrialOutcome.usd_elsewhere` (I-095); Playwright and the CI browser step; `test_product_evals_fake.py`.
- `Toolchain.skipped_only`, the `no tests ran` result, verify's refusal and hint, the acceptance and grader checks (I-097); `test_skipped_only.py`.
- The PR-body and router budget guards and `tests/unit/test_budget_guards.py` (I-098); `Settings.claude_code_token_factor`, `token_factor()`, `BudgetsConfig.scaled(tokens=)` and the cap-ratio assertion (I-099).
- ADR 0015; CONTRIBUTING steps 5 and 6; ledger I-085 to I-099; EVAL_HISTORY rows for the live product runs.

## Post-M11 follow-ups (2026-10-27, issue #58, ADR 0016, PR #59)

### Lessons applied
- Inventory first (M6 to M11): a read-only sweep of the open items in ADRs 0013 to 0015 and the `--gh` path became the issue body and its six slices, and found I-102 and I-103 before any code.
- The Fake only reproduces the shapes you thought of (I-031 to I-035): the GitHub-mode test had faked every merge as a fast-forward, so the I-102 test makes real squash merges on a bare origin (a new commit with the branch's tree) and failed against the old code before the fix.
- A node that interrupts does nothing else (I-065): `release_merge_wait` is its own node between `release` and `release_publish`.
- Green here, red there (I-074, I-080, I-084): the Docker and Node paths were written for CI's daemon and proven there, not on the dev machine.
- Measure before designing (M11): the parallel test run was timed on CI's runner (4 cores) before choosing the worker count.

### What succeeded
- Parallel CI tests: 789 s serial to 314 s on GitHub's 4-core runners, identical counts; the `checks` job has room again inside its 25-minute budget.
- The blind-written docker-live Node test (a real `npm install` through the allowlist proxy, `node --test` on TypeScript) passed on its first CI run, as did the image change behind it (I-101).
- `scio upgrade` on a real M9 product: `bookmarks_api` gained its Dockerfile, `.dockerignore`, README run block, coverage source and CI step in one commit, and its next `scio build` built M2 live (P5 to P7 passed, every integration check and acceptance green, $4.56 estimated, nothing billed); the upgraded README run block and release files passed the bar they were added for.
- `run_app` and the acceptance run now share one app-start module; the move also made a crashing app end the health wait early instead of waiting out the timeout.

### What failed
- Two `checks` runs failed (on `0554bf6` and `be9bb5a`) and I pushed two more slices before noticing: my CI watcher waited on the newest run only, and I did not look at the older ones (I-104). The cause was green-here-red-there once more: the dev machine's global git identity hid two commits that named none (`scio upgrade`'s and a test helper's), and the same gap existed in `scio build` on a product clone.
- I assumed the docker-live tests ran only in the `docker-live` job; both jobs have a Docker daemon, so the new Node test failed in `checks` on the plain Python image (I-105).

### Actions
- `sync_main`, `SyncRefused`, `BOOKKEEPING_MARKERS` and the two squash-merge tests (I-102); the `release` / `release_merge_wait` / `release_publish` split, `ReleaseRecord.pr_number` and `pr_url` (I-103).
- `scio/product/upgrade.py` and `scio upgrade`; `tests/unit/test_upgrade.py`.
- Node 22 in `docker/sandbox.Dockerfile`, `sandbox_probe` in both profiles, `Toolchain.sandbox_probe_argv`, the stack-aware `product_sandbox_backend`, `tests/unit/test_product_sandbox_backend.py` and the docker-live Node test (I-101).
- `scio/aci/apps.py`; `run_app` on any sandbox; `test_run_app_on_a_non_local_sandbox_*` and the docker-live `run_app` test.
- `SCIO_GIT_IDENTITY` on every commit, merge and tag SCIO makes; the autouse `_ci_git_identity` fixture in `tests/conftest.py`, shown to fail the old `upgrade.py` locally (I-104).
- The env-conditional skip of the Node docker-live test (I-105).
- CONTRIBUTING, "Tests and the environment": the CI-like git rule, check the previous push's CI before the next push, and the Docker-in-both-jobs rule (I-104, I-105).
- pytest-xdist in the dev group and `pytest -n` in CI (I-094 follow-up); ADR 0016; ledger I-101 to I-105.

## The dashboard (2026-10-27, issue #68, ADR 0017)

### Lessons applied
- Measure before designing (pre-flight 8): two probes ran before the contract was written. One opened a `mode=ro` connection under LangGraph's `SqliteSaver` on a WAL checkpoint DB, with a writer open and closed. The other dumped a Fake build's checkpoint channels, pending writes and interrupts. The data layer's whole read-only design, and the `branch:to:<node>` rule for the current step, rest on them.
- Contract first, then a parallel wave with owned files (the owner's orchestration rules): `scio/dashboard/contract.py`, the fixtures from a real Fake build, and a fixture-backed `DataSource` came first, so the data, API, page and docs agents never waited on each other. The page and the API were written by different agents against the same JSON and met for the first time in `test_the_real_app_serves_the_page_live_and_stops_cleanly`, which passed on its first run.
- Read a checkpoint with its own kind's model, classified by channels (I-066, I-108): the dashboard reuses `scio report`'s builders, so its costs equal `scio report`'s to the cent on the live graphcalc build ($9.0983, every feature row and role identical).
- One live check before claiming done (M5): the live run found two things no Fake test could, the attempt in progress missing from the build view and its stopped status.

### What succeeded
- The plan's 3 to 3.5 hours held: wave 0 (probes, contract, fixtures) about 40 minutes; wave 1's four agents in parallel in 6 (docs), 15 (API), 19 (data) and 35 (page) minutes against an estimate of 90; integration, the live checks and the fixes about 70 minutes.
- Reviewing the contract against the probes before fan-out caught I-120 (a stop never moves the checkpoint id) when it was a docstring change, not a cross-agent rewrite.
- Each agent's sweep found something outside its own file: a torn ledger line that would crash `scio report` on a live run (I-127), worker turns with no trace spans (I-122), an install hint that would uninstall extras (I-124).
- Mutation checks made the new guards honest: the WebSocket diff, the Origin check and the MIME pin (agent B), the shutdown test against a send-only handler (I-130), the torn-line test against the old loader (I-127).

### What failed
- The plan's change detection was wrong (I-120): it polled checkpoint ids, and a stop is a pending write on an existing checkpoint.
- My briefs carried two mistakes that agents corrected: Phoenix's OTLP port (I-125) and a bare `uv sync --extra` (I-124). One prediction of mine was refuted by a test (I-121).
- A shutdown hang appeared only under the full parallel run (I-132): uvicorn's last step waits on `asyncio.Server.wait_closed`, which on Windows can wait forever for a reset connection. Three reproductions and a task dump found it; the fix is one argument.
- My first live-check script orphaned its server: `terminate()` stopped the `uv run` wrapper, not the Python child holding the pipe. The script for the second check runs the interpreter directly.
- Agent C ran one read-only `git config` against "never run git", and a shell one-liner with a regex was mangled again (I-131).
- The fresh-context review found five things every test and both live checks had passed, each confirmed by a probe:
  - an answered stop still read "waiting" until its node returned (I-134);
  - a resumed crash still reads "crashed", which the DB cannot tell apart, so it is documented (I-133);
  - one unreadable root emptied the whole list (I-135);
  - T2's explorers lit no step (I-136);
  - a running attempt's spend lagged its ledger (I-137);
  - any local page could watch the WebSocket (I-138).

  Two of the tests it named passed by construction: an empty `next_nodes` satisfied a subset check, and a test set the checkpoint and ledger spend equal. A reviewer that is handed the code and not my conclusions is worth its 20 minutes.

### Actions
- `scio/dashboard/` (contract, data, app, static) and `scio dashboard` with `DASHBOARD_HOST` and `DASHBOARD_SHUTDOWN_S`; the `dashboard` extra.
- Tests: `test_dashboard_contract.py`, `test_dashboard_data.py`, `test_dashboard_app.py`, `test_dashboard_cli.py`, `tests/live/test_dashboard_page.py`, `tests/live/test_dashboard_server.py`, `test_lf_writes.py`, the torn-line test in `test_m5_observability.py`, `test_report_reads_a_build_that_stopped_before_its_first_feature`.
- `CostLedger.load_entries` reads complete lines only; `_read_artifact` replaces a broken character (I-127).
- The review's fixes: the answered-stop rule and `test_every_node_interrupts_at_most_once`, `Send` packets in `next_nodes`, the running spend from the ledger, `FailureStreaks` and the 503 for an unreadable root, `is_same_origin`, `FrameDenyMiddleware`.
- ADR 0017, `docs/OBSERVABILITY.md`, README, CHANGELOG, PRODUCT_MODE, ARCHITECTURE's tree; ledger I-120 to I-139; guardrails 20 to 34.

### Bottleneck audit
- Slowest steps: the page agent (35 minutes, most of it Playwright waits and screenshots), diagnosing I-132 (about 15 minutes: three full runs and an instrumented one), and agent D's second round, which waited on the other agents' facts.
- Ran one after another but could have run in parallel: the live check waited for the running-feature fix; the snapshot-based check could have started as soon as the API and page were committed. The docs agent's second round could have been briefed with placeholders in its first.
- Repeated friction: lint findings on first drafts in every agent (E501, SIM105, SIM117, RUF100), all fixed before a commit; Windows-only behaviour once more (CRLF, the proactor's reset connection).
- Improvements: (1) the `_Server` helper in `tests/live/test_dashboard_server.py` runs uvicorn exactly as the CLI does, so a future server test starts from it; (2) run the full parallel test set at each gate, not only the new files: I-132 showed only under load, and I-139 only in a repository-wide scan.

## The graphcalc run's S7 to S10 (2026-11-04, issues #77 and #79, PRs #78 and #80)

### Lessons applied
- Read this file end to end first. M8's "a node that interrupts does nothing else" (I-065) shaped the merge stop's refusal: the stop only reads PRODUCT.md before it asks, and it refuses after the answer, with no effect in between. I-061 (drive the CLI, not the library) put the S10 regression test through `scio build`, which is how the CLI's own parse (`scio/cli.py:144`) came to light before it could block the refusal. I-099 (a guard needs its own measurement per quantity) framed S9: the agent measured Claude Code turns from the run's session logs instead of guessing a cap.
- The orchestration rules: the blocking fix (S10) shipped alone first, and S7's client half, S8 and S9 ran as three worktree agents with disjoint owned files while S10 was written; the shared files (the ledger, this file, the CHANGELOG) were written after the wave from the agents' reports.
- The advisor's review before declaring done caught a third: the reused plan skipped the normaliser's ordering rule (I-046), so a feature inserted ahead of a halted milestone would have waited a milestone.
- The advisor's review of the plan before any code caught two wrong turns: the CLI's pre-parse (the refusal would have been unreachable), and "resume `progress.build_thread`" as S7's fix (graphcalc's `progress.json` still names the thread abandoned mid-P5).

### What succeeded
- Every fix landed with a regression test shown to fail on the old code, and the live run was checked read-only on copies: graphcalc's 26-feature PRODUCT.md parses, its paused `build-graphcalc-m2` checkpoint loads under the new models with `merge_wait` next, and the S7 client's listing was checked against the real repository.
- The agents' sweeps found more than their own bug: the advisor's `revised_steps` replan dropping the localized files (fixed), the T1 coder and debugger having S9's carry-over gap (fixed), `lstrip("./")` in two modules (I-160), and 7 of the run's 10 explorer summaries being invalid JSON (I-152).

### What failed
- The first S10 gate failed on the docs check reading the agents' own worktrees under `.claude/worktrees/` (I-155): running worktree agents inside the repository changes what a repository-wide scan sees.
- Every agent's first mypy run failed on a fresh, extras-less environment (I-156), and one of my own edits went through a shell script with a backslash again (I-157, caught by its anchor assert).
- My first plan-reuse change crashed a resumed build on a commit with nothing staged (I-158). The full product-build suite caught it; the two new tests alone did not.

### Actions
- `ProductSpec.features` without a cap, `product_md_problem`, `ProductMdUnreadable`, `declared_stack`, the merge stop's refusal (I-147, I-148); `stored_plan`, `plan_path`, `_start_milestone`, the `Mn: ` milestone lookup (I-149); `find_issue` by listing, `find_milestone_by_prefix` and the stricter `FakeGh` (I-150); `localized_files`, `add_localized_files`, `keep_localized_files`, `summary_text` (I-151, I-152); `claude_code_max_turns`, `T0_TURN_CAP`, `DOCUMENTER_TURN_CAP` (I-153); `previous_session_block`, `previous_on_step` (I-154); `_skip`'s `NEVER_OURS` and the `.gitignore` line (I-155); the staged check in `_commit_changed` (I-158); prefix normalisation in `_norm` and `is_test_path` (I-160).
- Ledger I-147 to I-160 (I-159 open: drift still flags non-source files the explorer named); guardrails 45 to 55, and guardrail 17 now cites I-157; ADR 0011 decision 4 and ADR 0012 decision 2 amended; ARCHITECTURE.md's T0, coder and localize lines.

### Bottleneck audit
- Slowest steps: the full local gate (about 18 minutes each time: 9.5 minutes of tests at 8 workers while three agents ran their own suites, then about 8 minutes of regression evals), run twice for S10 because of I-155; the S8 and S9 agents (35 and 38 minutes, most of it their own test runs and the S9 measurement); PR #78's CI (13 minutes).
- Ran one after another but could have run in parallel: the first gate ran while the agents were still running tests, which made it slower and exposed I-155; the S7 plan reuse waited for #78's gate because both touch `scio/product/build.py`. Splitting `plan_milestone` out of `build.py` first would have let it go to a fourth agent.
- Repeated friction: fresh worktree environments (I-156), lint on first drafts (E501, RUF043), and Windows git quirks (I-158's status listing, not yet explained).
- Improvements: (1) every worktree agent's brief starts with `uv sync --frozen --all-extras --group dev` (guardrail 54); (2) `.claude/worktrees/` is git-ignored and skipped by the docs check (I-155), so a gate run while agents work no longer fails on their checkouts.

## Safe mode and faster gates (2026-11-08, issues #87 and #88, PR #89 and the #88 PR)

### Lessons applied
- Read this file end to end first. The S7 to S10 audit named the gate as the slowest step (about 18 minutes with agents running) and asked for measurement before change (I-099), so every change here started from a number: the 40 slowest tests, the CI job's step times, the machine's 24 cores.
- I-104 (a watcher on the newest run reports the previous head) shaped `scripts/watch_ci.py`: it waits for the run whose `headSha` is the pushed commit. Guardrail 44 (`python -m`, never a venv launcher) kept every gate step on the interpreter, and its test now loads the gate itself.
- The advisor's question while planning this pass ("does graphcalc's workspace CLAUDE.md reach the coder sessions?") found I-171, the run's largest bug so far, before any gate work started; it shipped first, alone, as #89.

### What succeeded
- The local gate went from about 9.5 minutes (the 8-worker chain, one step after another) to 4.7 and 4.9 (`scripts/gate.py`, two runs; lint and types take seconds). The tests at 16 workers ran 262 s and 251 s alone, against 342 s at 8. Both runs reported 563 passed and 18 skipped, the same counts, with no failure that appears only in parallel. Beside the tests, the regression evals take 2.1 minutes and stay off the critical path; alone, with a task's trials run at once (`--parallel 5`), they take 67 s, and pass^5 stayed 1.00 every time.
- CI's old `checks` job ran the tests (10 min 4 s on #89) and then the evals (3 min 54 s). The new `tests` and `evals` jobs run side by side, and an aggregator keeps the status name the ruleset requires. On this PR's head (`15d50cc`) the whole run took 7 min 31 s against #89's 14 min 46 s: tests 6 min 43 s, and beside them evals 2 min 1 s, with 5 trials at once on the 4-core runner and pass^5 = 1.00. The regression suite (t001 to t010) needs neither Node nor Chromium, so the evals job installs neither and loses nothing.
- Guardrail 17, the rule broken most often (eight ledger ids), now has a running guard: the push-gate hook refuses a backslash in a heredoc or a `python -c` script.
- A cache of the bootstrapped product was measured before it was built, and it was not built. One `_bootstrap` (`scio new` on the Fake) takes 0.9 s on three runs, so the 32 tests that bootstrap spend about 29 s of worker time on it, about 2 s of wall time at 16 workers. Their 20 to 89 s goes to the builds.

### What failed
- The first use of the new watcher missed #89's running CI: a short SHA never matches GitHub's full one (I-172).
- That same line piped the watcher through `tail` without pipefail and printed `EXIT=0` over its exit 2 (I-173). The sweep ran every Bash command of this session through the new check: 103 of 708 piped a check without pipefail, 19 of them into an `&&` chain. Pre-flight 10 (I-018) was a rule held by habit, and the habit had lapsed. No failure was masked into `main`, since CI reran every check unpiped.
- Two first-draft errors in the new scripts: a dataclass in a module loaded by path (I-174) and `# fmt: skip` inside a bracket (I-175).
- While reading this PR's CI timings, a jq filter with `\\(` broke, a lapse of guardrail 17 in the very pass that guarded it. The cause, now measured for the first time, is that the Bash tool halves a double backslash before the shell runs (I-176). The hook now refuses any double backslash.

### Actions
- `SAFE_MODE` on every `claude` argv (I-171, guardrail 62); `scripts/gate.py`, with `tests/unit/test_gate.py` showing that a failed step fails it and that its eval flags are CI's; `scripts/watch_ci.py` with `full_sha` (I-172); `backslash_reason` (heredocs, `python -c`, any double backslash) and `pipefail_reason` in `scripts/hooks/push_gate.py` (guardrail 17, I-173, I-176); the CI split into `tests`, `evals` and the `checks` aggregator; CLAUDE.md's CI line and CONTRIBUTING's test section name the gate, 16 workers and the watcher.
- Ledger I-171 to I-177; guardrails 62 to 66, and guardrail 17's guard is now `hook:push_gate`.

### Bottleneck audit
- Slowest steps: CI's tests job, now the whole critical path (its tests step took 6.7 to 10 minutes over the last three runs: 403 s on this PR, 604 s on #89, 459 s on 2026-11-07), the local tests at 16 workers (4.2 to 4.7 minutes), the product-build Fake tests inside them (20 to 89 s each; 32 tests bootstrap a product first).
- Ran one after another but could have run in parallel: the local tests and evals, and CI's tests and evals (both fixed here); the measurement runs of this pass ran in one background chain while the docs were written.
- Repeated friction: a hook fix protects only after it merges, because the session runs the main checkout's hook; the pipefail habit continued while the check that refuses it was being written. Lint failed on first drafts again (RUF028, then an E501 in the hook's own message), each caught before its commit by a chain that could fail.
- Improvements: (1) `scripts/gate.py` replaces the chained line, so no check is piped at all (I-173); (2) the next cut is CI's tests job, whose runner has 4 cores (`nproc` in #89's log) against this machine's 24. Running the product-build tests as a job of their own is the next thing to measure.

### After the merge (issue #91)
- The live check of the new hook found two things. The session had restarted outside the repository and was running no hooks at all (I-178, open, the owner's decision). Fed by hand, the hook script also showed that its push and commit gates had failed open on every `cd /c/...` line: 8 commits unlinted and 3 pushes unchecked in this pass, each with a warning nobody read (I-179, fixed).
- Verified live on 2026-11-13, in a session started in the SCIO directory: a `printf %s 'x\\y'`, a backslash heredoc and a piped `mypy | tail -1` were each denied with the hook's message. That confirms I-176's premise, that the hook sees a command before the Bash tool halves it. After `cd /c/...`, a `git push --dry-run` went through with no "could not check" warning (I-179).
- I-178 resolved by the owner's decision (issue #93): the user settings run the hook from `.scio/live-wt` in every session. A `claude -p` session started in a non-repository folder had its bare `git stash` denied.

## The self-healing supervisor and its live drill (2026-11-14, issue #95, PRs #96, #108, #110, #111, #112)

### Lessons applied
- Pre-flight 8 (measure before building) shaped ADR 0019 §12a. Process survival, Task Scheduler's PATH (no `sh`, I-183) and the job object were measured with throwaway tasks before any supervisor code was written.
- I-104 and guardrail 63 meant every CI wait watched its own head by SHA. When GitHub made a commit (`gh pr update-branch`), the watcher's local lookup crashed, so watch_ci now takes a full SHA as given (I-199).
- Guardrail 17 held twice where it was tested: the hook refused a backslash heredoc (I-191, I-203) before anything was written.

### What succeeded
- **The drill passed end to end, on the real defect (I-180) and without anyone typing a tick.**
  - 09:48:41: the M2 planner's `claude -p` call hung under an injected 1 s timeout, and `scio build` ended in a `TimeoutExpired` traceback.
  - 09:48:56 to 09:49:01: the supervisor detected it, preserved 11 items, classified it `harness_suspect` at `claude_code.py:run`, opened issue #105 and started repair attempt 1.
  - 10:40:26: the repair session ended with PR #110. It fixed I-180 and I-182, its sweep found I-190 to I-192, and it had green CI, a test and a ledger entry. The supervisor checked those claims on GitHub (`pr_verified`) and waited.
  - 11:23:37: the owner's pre-approved merge.
  - 11:32:01: the scheduled tick saw the merge and held for main's CI.
  - 11:42:03 to 11:42:07: the next tick moved live-wt from `1019c21` to `e84e6a8`, passed the health check, and replayed `build --product product` with `--decision` dropped.
  - 11:48:23: the replay built M2 (P4, P5) and stopped at M2's merge stop. The incident went `verified`, then `closed`.
- The product's history is clean. `main` holds exactly one `[scio:merge:M1]` and one `[scio:progress:M1]`, and each M1 feature once. `milestone/2` added P4 and P5 once each. The replay cost $3.68.
- The scheduled tick was registered (issue #98) and ran from Task Scheduler with `LastTaskResult` 0. It, not a person, carried the incident from `awaiting_merge` to `closed`.
- `python -m recovery watch --follow` (#107) showed every state change live, so the drill needed no polling of the store by hand.

### What failed
- The repair session's figures in the incident timeline are wrong: 4 turns and 34 s, against 51.3 minutes, 127 turns and 154 tool calls in its transcript (I-195, open).
- The repair session's sweep found I-190 to I-192, but its I-192 entry had no status line (I-204). Ledger hygiene is not in the repair brief's checks.
- CI caught what the Windows gate could not: `recovery watch` split a Windows path with PosixPath on Linux (I-194).
- `watch_ci.py` crashed on a commit GitHub had made (I-199).
- The documented tick recipe was refused without an administrator (I-200), because the measurement in §12a skipped its logon trigger. The first draft of its guard read nothing (I-201).
- A gate failed on a dashboard test that passes alone. Its root cause was a race in the `/ws` loop, which cancelled its read of the socket every poll (I-202).

### Actions
- `recovery/` (ADR 0019, #96), `python -m recovery watch` (#108), `scio/proctree.py::run_tree` and `harness_stop_line` (#110, I-180, I-182, I-189), the commit gate's scope (#111, I-186, I-193), and `scripts/watch_ci.py` taking GitHub's SHA as given (I-199).
- The dashboard's recovery view and `_until_disconnect` (#112, I-196, I-197, I-202).
- The scheduled task "SCIO recovery tick" is registered on the dev machine. Its recipe, `check_scheduled_task_recipes`, the wider `FENCE_RE` and `check_ledger_statuses` are on the #98 branch (I-200, I-201, I-204).
- I-195 is recorded, open.
- Guardrails 69 to 80 and 83 are on `main`. 81, 82 and 84 are on the #98 branch.

### Bottleneck audit
- **Slowest steps:**
  - The repair session: 51.3 minutes.
  - CI's tests job: the last four runs took 9.7, 13.1, 13.6 and about 13 minutes.
  - The local gate: 6.4 to 7.0 minutes, five times.
  - From the merge to the tick moving live-wt: 18.4 minutes. That is main's CI, 13.6, plus 4.7 waiting for the next tick.
- **Serial work that could have run in parallel:** the merge queue. The ruleset requires a branch to be up to date with `main`, so every merge (#110, #111, #112, the release) cost the next PR a merge of main, a full gate and a CI run, about 20 minutes each, one after another. Gates and CI did overlap where they could: one PR's gate ran beside another's CI, and the I-202 repro ran beside a gate, where the load reproduced it.
- **Repeated friction:** the same three files conflicted at every merge: `ISSUE_LEDGER.md`, `LESSONS_LEARNED.md` and `CHANGELOG.md`. A small resolver, which keeps both sides' ledger entries in id order, was written once and used three times.
- **Improvements:**
  1. Move the ledger resolver into `scripts/` with a test, so a merge's ledger conflict is one command.
  2. Fix I-195, so the next repair session's cost and length are read from its transcript.

## Workflow v2 (2026-11-14 to 11-15, issue #122, PRs #125, #127, #128, #131, #132, #135, #138 and the W-44 PR)

The owner asked for every change from the workflow teardown, tracked in a checklist that every session reads until all of it is in. From the approval (21:12 UTC) to the last item took about 5.5 hours, with one stop for a used-up usage window. The checklist's 45 items: 43 done, W-42 superseded by the fragments, and W-44 dropped on the owner's word.

### What succeeded
- **The checklist computed itself.** `scripts/workflow_status.py` derived every status from evidence in the tree, and no PR edited the checklist to record progress, so it never conflicted. The `SessionStart` hook showed the open items in every session, including after a context compaction.
- **Agents ran in parallel worktrees, each from a brief.** `scripts/brief.py` filled the brief, and each agent's report went to `.scio/agents/` (W-34). The PR bodies and ledger entries were built from those reports.
- **The fragments removed the shared-file conflicts.** From #138 on, a ledger entry, a rule or a change note is its own file. The migration moved 217 entries, 93 rules and 14 changes byte for byte, and a rerun writes nothing.
- **Gate v2.** The gate is quick by default, stamps the tree it passed, and is enforced by the push hook. `--full` ran where it had to: the fragments PR's full gate took 8.2 minutes, with 1297 passed and evals pass^5 1.00.

### What failed
- **Rewording CLAUDE.md broke a test** that finds the gate line by its words (I-218).
- **Browser tests timed out** beside the unit tests' workers once CI was sharded (I-224).
- **A push was rejected** because a GitHub update-branch commit had not been fetched (I-225).
- **An Edit landed in the main checkout** instead of the worktree (I-226).
- **W-44 was planned for a feature this repository cannot have.** GitHub offers merge queues only to organization-owned repositories (I-227).
- **Inline scripts with backslashes** were refused 4 times by the hook (`hook-refusals.log`: backslash 2, double-backslash 2), and one push was refused for want of a gate stamp (push-gate 1).

### Actions
- `docs/WORKFLOW_V2.md`, `scripts/workflow_status.py` and the `SessionStart` and `Stop` hooks (#125).
- CI shards and the evals gate (#127, #135). The dev helpers and the agent brief and report templates (#128). `session_state`, `next_ids`, `land` and `move_live_wt` (#131). Gate v2 and its stamp (#132).
- The fragments, `ledger_new.py` and `migrate_fragments.py` (#138).
- `scripts/hooks/worktree_guard.py` (I-226, guardrail 99). `.claude/templates/research-brief.md` (I-227, guardrail 100).
- Guardrails 85 to 100. I-217 and I-223 stay open.

### Bottleneck audit
- **Slowest steps:**
  - The full gate: 8.2 minutes.
  - CI: about 10 minutes a run.
  - #128 waited about 2 hours from open to merge, across the stop for the usage window.
  - Every other #122 PR merged 5 to 20 minutes after it opened.
- **Serial work that could have run in parallel:** each merge still forces the next PR through an update-branch and a new CI run, because the ruleset requires an up-to-date branch. Without a merge queue (I-227), `land.py --next` is what overlaps one PR's gate with the previous one's CI.
- **Repeated friction:** before the fragments, every PR conflicted on the ledger, LESSONS and CHANGELOG, and `merge_ids.py` was run by hand at each merge. From #138 on, a PR adds files instead.
- **Improvements:**
  1. Land every queued PR with `scripts/land.py N --next`, so the next gate runs during this PR's CI.
  2. Close I-223: the provider raises a used-up usage window as its own error. The supervisor then reads it from the error's type, not from its wording.
