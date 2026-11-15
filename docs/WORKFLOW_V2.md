# Workflow v2: the checklist

The owner, 2026-11-14, after the workflow teardown of the v0.10.0 session: "lets make every chage you just sugested and make sure that the checklist is a doc that will continely be reference and check until all the chage have been implemented". Issue [#122](https://github.com/NewMe505/SCIO/issues/122) tracks it, and closes when every item below is closed.

## How this checklist works

- **Nobody ticks a box.** Each item names its evidence: a test, a symbol, a file, or a line in CLAUDE.md. `uv run python scripts/workflow_status.py` computes from the tree whether that evidence holds. An item is done exactly when all its evidence holds. So the checklist cannot claim work that is not there, and PRs never edit it to record progress; it would conflict on every merge.
- **Every session sees it.** The `SessionStart` hook in `.claude/settings.json` runs `scripts/workflow_status.py --session`. It puts the count and the next open items into the session's context, until every item is closed. `scripts/session_state.py` (W-20) prints them too.
- **The docs check keeps it honest.** `check_workflow_checklist` fails on a malformed item, a gap in the ids (an item deleted without a trace), a `Done:` line on an item whose evidence is checkable, or a superseded item that points nowhere.
- **The only edits after it lands:**
  - a `- Done: <date> · <evidence>` line on a `manual:` item (a GitHub setting, or a file outside this repository);
  - `- Superseded by: W-NN`, when another item covers it;
  - `- Dropped: <reason>`, only on the owner's word.

  An item's evidence may also be corrected when a PR names its test differently. The PR must say so.
- **Evidence kinds:**
  - `grep:<path>:<literal>`: the file contains the literal text.
  - `file:<path>`: the file exists.
  - `symbol:<path>:<name>`: the file defines `name`.
  - `test:<name>`: a test function exists under `tests/`.
  - `manual:<reason>`: closed only by a `Done:` line.
- **What fragments won't fix:** README's status line and roadmap are one shared block, so two PRs that both change them still conflict. Keep README edits to the PR that changes the status.

Every suggestion from the teardown is here. Duplicates name each other: the PR body generator was listed twice, and is W-24. `merge_ledger.py` is superseded by the fragments (W-42).

## Process rules (CLAUDE.md)

### W-01 · Write a problem's ledger entry before starting the gate that carries it
- From: teardown 2, Cut 4 and the instant list. Two #98 gate starts and one #117 re-gate were thrown away for entries written mid-gate.
- Change: a CLAUDE.md rule.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:Write a problem's ledger entry before starting the gate that carries it`

### W-02 · Live status stays out of gated files
- From: teardown 1 (I-208's "none has halted yet" was false within the hour and cost a re-gate and a CI restart).
- Change: a CLAUDE.md rule. Live status goes in PR comments.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:goes in a PR comment, never in a gated file`

### W-03 · Read the workspace before asking the owner
- From: teardown 2, Cut 8 (the stopgap question whose answer was already in graphcalc's `scio.sh`).
- Change: a CLAUDE.md rule.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:before asking the owner anything they may already have answered`

### W-04 · Approvals are batched and recorded in .scio/approvals.md
- From: teardown 2, Cut 7, and Context 2 (#114 sat ready for an hour; "mege for me" needed a follow-up question).
- Change: a CLAUDE.md rule: ask once per batch, naming the issues or a `Refs #N` set, and record the owner's words in `.scio/approvals.md`. `land.py` reads it (W-23).
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:.scio/approvals.md`

### W-05 · Inspection scripts live in files, never inline
- From: teardown 2, Automate 7 (three hook refusals this session: I-207, I-212).
- Change: a CLAUDE.md rule; the helpers themselves are W-30.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:Inspection scripts live in scripts/dev/ or the scratchpad`

### W-06 · watch_ci finds a run by its commit
- From: teardown 1 (I-211, a 300 s timeout).
- Change: `scripts/watch_ci.py` without `--branch` looks the run up by commit.
- PR: #118 (merged)
- Evidence: `test:test_without_a_branch_the_run_is_found_by_its_commit_not_the_checkouts_branch`

### W-07 · Branch from main; stack only when forced
- From: teardown 2, Cut 5 (#117's retarget, update-branch and CI rerun).
- Change: a CLAUDE.md rule.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:Branch from main; stack a PR on another only when a conflict forces it`

### W-08 · Agents report open questions, never owner decisions; the owner's invariants become named tests
- From: teardown 2, Quality 1 and 2 (#116's draft called its own gap an "owner decision" and broke "halt only after that fails").
- Change: a CLAUDE.md rule; the template is W-32.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:An agent reports open questions, never owner decisions`

### W-09 · A research agent pairs with work that depends on provider or CLI behaviour
- From: teardown 2, Parallelize 6 (the `--resume` and PowerShell probes).
- Change: a CLAUDE.md rule.
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:pair it with a research agent`

## The checklist itself

### W-10 · This checklist is computed, shown at every session start, and checked
- From: the owner's request, 2026-11-14.
- Change: `scripts/workflow_status.py`, the `SessionStart` hook, `check_workflow_checklist` in the docs check, and the CLAUDE.md pointer.
- PR: the checklist PR
- Evidence: `symbol:scripts/workflow_status.py:main`, `test:test_the_checklist_status_is_computed_from_evidence_not_ticked`, `grep:.claude/settings.json:workflow_status.py`, `grep:scripts/check_docs.py:check_workflow_checklist()`, `grep:CLAUDE.md:docs/WORKFLOW_V2.md`

## Gate v2 (scripts/gate.py, scripts/hooks/push_gate.py)

The contract the gate writes, and the hook, `land.py` and `session_state.py` read: `.scio/gate/last-pass.json` holds `{"tree": <git tree id>, "head": <commit>, "mode": "full" | "quick" | "docs", "at": <UTC ISO>, "counts": {...}}`.

### W-11 · The gate writes a stamp on a pass, and a dead gate leaves a marker
- From: teardown 2, Automate 3, and Context 4 (the 12:07 gate died at 88% and left no trace).
- Change: the stamp above, written on a pass. `.scio/gate/RUNNING` exists while a gate runs and is removed when it ends.
- PR: gate v2
- Evidence: `symbol:scripts/gate.py:write_stamp`, `test:test_a_pass_writes_the_stamp_and_a_dead_gate_leaves_its_running_marker`

### W-12 · A tree that already passed is not gated again
- From: teardown 2, Cut 2 (merge commits with an identical tree).
- Change: the gate exits 0 at once when the stamp's tree equals HEAD's tree and the worktree is clean.
- PR: gate v2
- Evidence: `test:test_a_tree_that_already_passed_is_not_gated_again`

### W-13 · A docs-only change runs only the docs steps
- From: teardown 2, Cut 1 (I-208's one-paragraph correction cost a 6.5-min gate).
- Change: when every path changed since the stamp's tree is Markdown, only the docs check and the tests that read docs run, in about 20 s.
- PR: gate v2
- Evidence: `test:test_a_docs_only_change_since_a_gated_tree_runs_only_the_docs_steps`

### W-14 · The gate is quick by default, full on demand; CI runs the full suite
- From: teardown 5, Heavy (the local full gate caught 0 code defects in 5 passes this session). The owner approved every change.
- Change:
  - `gate.py` runs lint, types, the tests of the changed paths, and the docs check.
  - `gate.py --full` runs everything, as before. It is used for a release cut, after a red CI, and when the change touches the gate itself.
  - CLAUDE.md and CONTRIBUTING say so.
- PR: gate v2
- Evidence: `test:test_the_quick_gate_runs_the_tests_of_the_changed_paths`, `grep:CLAUDE.md:gate.py --full`

### W-15 · The gate runs the evals only when code changed
- From: teardown 2, Parallelize 4.
- Change: the eval step runs when `scio/`, `evals/`, `configs/` or `prompts/` changed since the stamp, or with `--full`.
- PR: gate v2
- Evidence: `test:test_the_gate_skips_the_evals_when_no_code_changed`

### W-16 · One gate at a time on this machine
- From: teardown 2, Parallelize 7 (gates and agents' test runs under load at once).
- Change: a machine-wide lock outside any repository. A second gate waits and says so.
- PR: gate v2
- Evidence: `test:test_a_second_gate_waits_for_the_machine_lock`

### W-17 · The push gate refuses an ungated tree, safely
- From: teardown 2, Automate 3 (turns "gate before every push" from a habit into a check).
- Change: `git push` of a branch is refused unless a stamp holds the pushed head's tree. These still pass:
  - tags;
  - a merge commit whose tree equals a gated tree;
  - a docs-mode stamp for a docs-only change;
  - the override `SCIO_ACK_GATE=<reason>`;
  - the recovery repair session's worktree, which runs its own gate.
- PR: gate v2
- Evidence: `test:test_the_push_gate_refuses_an_ungated_tree`, `test:test_the_push_gate_lets_a_tag_a_gated_merge_and_an_override_through`

### W-18 · A hook-blocked habit lapse is logged, not a ledger entry
- From: teardown 2, Cut 6. The owner approved every change, including this one to CLAUDE.md protocol 1.
- Change: the hook appends each refusal to a log outside any repository. A lapse the hook refused, with nothing getting through, goes to that log instead of the ledger, and the retrospective reports its count.
- PR: gate v2
- Evidence: `test:test_a_refusal_is_logged_outside_the_repository`, `grep:CLAUDE.md:hook-refusals.log`

### W-19 · A backslash refusal names a scratch file to write instead
- From: teardown 2, Quality 7.
- Change: the refusal message names a ready path under the session's scratch or `scripts/dev/`.
- PR: gate v2
- Evidence: `test:test_a_backslash_refusal_names_a_scratch_file_to_write`

## Session tools (scripts/)

### W-20 · session_state.py: the state-first inspection in one command
- From: teardown 2, Automate 1 (about ten manual commands per session; the stale handoff).
- Change: `scripts/session_state.py` prints:
  - main and the pinned checkouts;
  - each worktree's branch, ahead and behind, gated or not, and dirty count;
  - open PRs with their CI and merge state;
  - open `live-run` issues and recovery incidents;
  - the next ledger id and guardrail;
  - the open checklist items.
- PR: session tools
- Evidence: `symbol:scripts/session_state.py:main`, `test:test_session_state_reports_worktrees_prs_ids_and_open_checklist_items`

### W-21 · The next ids come from every ref, never from memory
- From: teardown 2, Automate 2, and Context 3 (the handoff said "next I-205" while I-205 was in use).
- Change: `scripts/next_ids.py` reads every local and remote branch and every worktree's files.
- PR: session tools
- Evidence: `symbol:scripts/next_ids.py:next_ids`, `test:test_next_ids_reads_every_branch_and_worktree`

### W-22 · land.py lands a PR up to the merge
- From: teardown 2, Cut 3, and Automate 4.
- Change: `scripts/land.py N`:
  - retargets to main when the base branch merged;
  - updates the branch when it is behind, and requires the new tree to equal a gated tree;
  - requires a stamp for the head's tree;
  - watches CI by commit;
  - marks the PR ready.
- PR: session tools
- Evidence: `symbol:scripts/land.py:main`, `test:test_land_retargets_updates_checks_the_stamp_and_watches_by_commit`

### W-23 · land.py prints the merge only for an approved PR
- From: teardown 2, Context 2.
- Change: it reads `.scio/approvals.md`. An approved PR gets the exact merge command; any other gets "waiting for the owner's merge N".
- PR: session tools
- Evidence: `test:test_land_prints_the_merge_only_for_an_approved_pr`

### W-24 · PR bodies are generated from the stamp and the agent's report
- From: teardown 2, Cut 9 (also listed in the teardown's Automate list).
- Change: `land.py` writes the Test evidence block from the stamp, and the summary from `.scio/agents/<issue>.md` when it exists. No more placeholders filled in by hand.
- PR: session tools
- Evidence: `test:test_land_writes_the_pr_body_from_the_stamp`

### W-25 · After a merge, one command finishes the tail
- From: teardown 2, Cut 3, and teardown 4.
- Change: `land.py N --after-merge`:
  - checks `mergedAt` in its own step;
  - pulls the main checkout;
  - retargets and requeues the PRs based on N's branch;
  - queues a live-wt move when N touched runtime code (W-27);
  - regenerates the handoff facts (W-28).
- PR: session tools
- Evidence: `test:test_after_merge_checks_merged_at_pulls_and_retargets_dependents`

### W-26 · Landing a release PR tags it and checks the release
- From: teardown 2, Cut 10.
- Change: for a `chore(release)` PR, `--after-merge` tags the merge commit (annotated) and confirms `release.yml` and `gh release view`.
- PR: session tools
- Evidence: `test:test_landing_a_release_pr_tags_its_merge_and_checks_the_release`

### W-27 · live-wt moves when no supervised build runs
- From: teardown 2, Automate 8 ("move live-wt at the next stop" was a manual watch).
- Change: `scripts/move_live_wt.py --when-idle` waits until no workspace lease is held (`python -m recovery status`), then moves live-wt to main. It never moves recovery-wt; only the owner does.
- PR: session tools
- Evidence: `symbol:scripts/move_live_wt.py:main`, `test:test_live_wt_moves_only_when_no_supervised_build_runs`

### W-28 · The handoff's facts are generated
- From: teardown 2, Context 1 and 7.
- Change: a `Stop` hook writes `session_state.py --markdown` to `.scio/session-state.md`. The memory handoff keeps only decisions and approvals, and points there.
- PR: session tools
- Evidence: `grep:.claude/settings.json:session_state.py`, `test:test_session_state_markdown_is_the_handoff_facts_block`

### W-29 · The next queued branch is gated while CI runs
- From: teardown 2, Parallelize 1.
- Change: `land.py --next` starts the gate of the next branch in the queue while the current PR's CI runs.
- PR: session tools
- Evidence: `test:test_land_next_starts_the_next_branchs_gate_while_ci_runs`

## Dev helpers and agent templates

### W-30 · Inspection helpers in scripts/dev/
- From: teardown 2, Automate 7.
- Change: `read_stream.py` (a `claude -p` stream-json summary) and `ledger_pairs.py` (per-call usage in a feature ledger), from this session's scratch.
- PR: dev helpers
- Evidence: `file:scripts/dev/read_stream.py`, `file:scripts/dev/ledger_pairs.py`

### W-31 · A probe harness with SCIO's exact worker flags
- From: teardown 2, Quality 6.
- Change: `scripts/dev/claude_probe.py` runs one `claude -p` probe with the flags `ClaudeCodeWorker` uses, and records the result. It is the one-command way to check CLI behaviour instead of writing it from memory.
- PR: dev helpers
- Evidence: `symbol:scripts/dev/claude_probe.py:main`, `test:test_the_probe_harness_uses_scios_exact_worker_flags`

### W-32 · The agent brief template and report schema
- From: teardown 3A and 3B.
- Change: `.claude/templates/agent-brief.md` (invariants as named tests, owned files including docs touchpoints, rules, checks) and `.claude/templates/agent-report.md` (the report fields, `open_questions` instead of decisions).
- PR: dev helpers
- Evidence: `file:.claude/templates/agent-brief.md`, `file:.claude/templates/agent-report.md`

### W-33 · brief.py fills the brief from the issue
- From: teardown 2, Automate 10, Parallelize 5, and Quality 3.
- Change: `scripts/brief.py <issue>` fills the template: the issue's evidence, owned files from a grep, the docs touchpoints (config comments, ARCHITECTURE excerpts, dashboard text, README lines), and invariant slots.
- PR: dev helpers
- Evidence: `symbol:scripts/brief.py:main`, `test:test_brief_lists_owned_files_docs_touchpoints_and_invariant_tests`

### W-34 · Agent reports are saved to .scio/agents/
- From: teardown 2, Context 5.
- Change: a CLAUDE.md rule. The orchestrator saves each agent's report to `.scio/agents/<issue>.md`, and PR bodies and ledger entries are built from it (W-24).
- PR: the checklist PR
- Evidence: `grep:CLAUDE.md:.scio/agents/`

## CI (.github/workflows/ci.yml)

### W-35 · CI shards the tests
- From: teardown 2, Parallelize 3 (about 10 to 12 min on the critical path).
- Change: the tests job splits into shards that together cover every test directory. The `checks` aggregator requires them all.
- PR: CI
- Evidence: `test:test_ci_shards_cover_every_test_directory`

### W-36 · CI runs the evals only when code changed, by a job condition
- From: teardown 2, Parallelize 4.
- Change: a change-detection step plus a job-level `if:`, never a workflow `paths:` filter (the required `checks` would never report). The aggregator treats skipped evals as passing.
- PR: CI
- Evidence: `test:test_ci_skips_evals_only_by_a_job_condition_and_checks_accepts_it`

### W-37 · CI runs on the merge queue
- From: teardown 2, Parallelize 2 (it must be in place before W-44 turns the queue on).
- Change: a `merge_group:` trigger, with the aggregator reporting for it.
- PR: CI
- Evidence: `grep:.github/workflows/ci.yml:merge_group`, `test:test_ci_runs_on_the_merge_queue`

## Fragments (after #119 to #121 land)

### W-38 · Ledger entries are fragments
- From: teardown 2, Automate 6 (every PR conflicted on `ISSUE_LEDGER.md`; I-206).
- Change: one file per entry under `docs/ledger/`. Every reader is switched:
  - `check_guardrails`, `check_ledger_statuses` and the I-104 rule;
  - `recovery/brief.py` (the repair session writes entries);
  - the CLAUDE.md protocols.

  No assembled copy is committed.
- PR: fragments
- Evidence: `file:docs/ledger/README.md`, `test:test_every_ledger_reader_reads_the_fragments`

### W-39 · Guardrails are fragments
- From: teardown 2, Automate 6.
- Change: one file per rule under `docs/guardrails/`. `check_guardrails` reads them, and LESSONS_LEARNED links to the folder.
- PR: fragments
- Evidence: `test:test_guardrails_are_read_from_their_fragments`

### W-40 · CHANGELOG fragments are assembled at the release cut
- From: teardown 2, Automate 6.
- Change: one file per change under `changes/unreleased/`. `release_notes.py --cut` assembles them into the released section. `check_released_section_frozen` and `release.yml` are unchanged.
- PR: fragments
- Evidence: `test:test_release_cut_assembles_the_changelog_fragments`

### W-41 · ledger_new.py scaffolds an entry with its status line
- From: teardown 2, Cut 4, and Quality 5.
- Change: `scripts/ledger_new.py --title ... --where ...` writes the next id's fragment with every field and `Status: open`, and prints the id.
- PR: fragments
- Evidence: `symbol:scripts/ledger_new.py:main`, `test:test_ledger_new_writes_an_open_entry_with_its_status_line`

### W-42 · merge_ledger.py, a per-entry 3-way merge
- From: teardown 2, Quality 4.
- Change: not built. With fragments there are no ledger merges to resolve.
- Superseded by: W-38

### W-43 · The recovery repair session writes ledger fragments
- From: the fragments design (the repair session wrote I-189 to I-192 into the ledger).
- Change: `recovery/brief.py` tells the repair session to add `docs/ledger/I-NNN.md`. recovery-wt moves only on the owner's word, so the handoff asks for that move.
- PR: fragments
- Evidence: `grep:recovery/brief.py:docs/ledger/`

## Last: GitHub settings and the graphcalc workspace

### W-44 · The merge queue is on, and the merge rule follows it
- From: teardown 2, Parallelize 2. The owner approved every change; turning on the ruleset needs the owner's click if the classifier refuses the API call.
- Change:
  - Only after W-37 is in: the ruleset requires the merge queue.
  - Under the queue, the human act becomes the enqueue. The CLAUDE.md merge rule, guardrail 8's `mergedAt` step and the handoff's "never `--auto`" are updated to match.
- PR: last
- Evidence: `manual:the ruleset is a GitHub setting`, `grep:CLAUDE.md:merge queue`

### W-45 · graphcalc files its SCIO proposals as issues
- From: teardown 2, Automate 9 (the owner relayed two pastes between sessions this session).
- Change: a rule in `graphcalc-run/CLAUDE.md`: improvement proposals go to `gh issue create --repo NewMe505/SCIO --label proposal --label live-run`.
- PR: none; the workspace is outside this repository
- Evidence: `manual:the rule lives in graphcalc-run/CLAUDE.md, outside this repository`
- Done: 2026-11-14 · graphcalc-run/CLAUDE.md, "A proposal to improve SCIO" under the owner's rule 1. The `proposal` label was created in NewMe505/SCIO.
