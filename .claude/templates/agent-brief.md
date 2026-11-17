<!--
The brief an orchestrating session hands a sub-agent (issue #122, W-32).
`uv run python scripts/brief.py <issue>` fills the {{slots}} it can find in the issue and the
tree, and leaves a TODO where only the orchestrator can decide: the decided design, the test
that guards each invariant, and the Identity, Failure and Resume edge decisions (it fills Pins).
Fill every TODO and delete this comment before sending:
`uv run python scripts/brief.py --check FILE` exits 1 and names each TODO left.
A brief is the agent's only context: what it does not say, the agent does not know.
-->
# Brief: #{{issue}} {{title}}

## Goal and issue
{{goal}}

Issue: #{{issue}}. Your work is done when every invariant below has a passing test that fails
on the old code, and the checks below pass.

## Worktree, branch, base and sync
- Worktree: `{{worktree}}` (stay inside it)
- Branch: `{{branch}}`
- Base: `{{base}}`, synced at `{{base_sha}}`

## Parallel agents
{{parallel_agents}}

They work in sibling worktrees. Stay inside yours and inside your owned files.

## Read first
- `CLAUDE.md` and `CONTRIBUTING.md`
{{read_first}}

## Evidence (verbatim, with paths)
{{evidence}}

## Decided design (not to be reopened)
{{decided_design}}

This design is decided. If the code shows it cannot work, stop and report it under
`open_questions` with the evidence; do not build another design.

## Invariants
Each invariant is the owner's words, quoted, and maps to one named test that fails on the old
code. A test name here is the evidence the checklist and the PR cite: keep it exactly.

{{invariants}}

## Edge decisions (decided before sending)
The four cases a brief most often leaves open. Each is decided here: build what its line says,
and say what you did for each under `edge_decisions`. A case the line does not cover goes under
`open_questions`.

- **Identity** (is every id, key or file name this work creates unique across threads, milestones and reruns?): TODO
- **Failure** (what happens when an external call or write fails?): TODO
- **Resume** (what does a rerun, or a record stored before this change, do?): TODO
- **Pins** (tests outside the owned files that pin this behaviour; they are run before reporting, in the checks below):
{{pins}}

## Owned files
You may read any file. You may modify only these.

Code:
{{owned_code}}

Docs touchpoints (each line names text that describes the code you change; update it in the
same change, or list it under `docs_touchpoints_left`):
{{owned_docs}}

Tests:
{{owned_tests}}

## Not yours
The orchestrator owns the shared files and writes them after the wave, from your report:
`CLAUDE.md`, `CONTRIBUTING.md`, `docs/ISSUE_LEDGER.md`, `docs/LESSONS_LEARNED.md`,
`CHANGELOG.md`, `README.md`, `pyproject.toml`, `uv.lock`, `.claude/settings.json`.
Report what they need under `docs_touchpoints_left`.
The fragments are yours: write each problem's ledger entry, each rule and each change note as
its own file (`docs/ledger/I-NNN.md`, `docs/guardrails/NNN.md`, `changes/unreleased/`), at the
ids this brief assigns. Never write them into the shared files above.

## Rules
- **No git writes.** No commit, push, stash, reset, checkout of another branch, merge, rebase or
  tag. The orchestrator runs git.
- **LF line endings.** Python writes pass `newline="\n"`.
- **The backslash rule** (guardrail 17). Text with a backslash (Windows paths, regexes, `\n` in
  a literal) goes through the Edit or Write tool, never a heredoc or `python -c`. Inspection
  scripts live in `scripts/dev/` or the scratchpad, never inline.
- **Machine load.** Run only your own test files and the Pins, plus ruff and mypy, never
  the whole suite; at most `-n 4`. Other agents share this machine.
- **The probe rule.** Never write provider or CLI behaviour from memory. Check the Claude API
  reference (the `claude-api` skill), or run one probe with `scripts/dev/claude_probe.py`
  (Haiku, from a temporary directory outside the repository, with `ANTHROPIC_API_KEY` and
  `CLAUDECODE` unset). Tests never run a live `claude` or a live provider.
- **Model ids** live only in `configs/models.yaml`; code names roles.
- **Open questions, never owner decisions** (W-08). A gap you could not close goes under
  `open_questions`, with what you tried and the evidence. Never call your own gap an "owner
  decision", and never weaken an invariant to make a test pass.
- **Every invariant is a named test** that you ran against the old code and saw fail.
- Tests, lint and types are ground truth: fix the code, never the check.

## Checks
Run these before you report, and paste the counts:

```
export UV_SYSTEM_CERTS=1 SCIO_PROVIDER_OVERRIDE=fake
uv run ruff check . && uv run ruff format --check . && uv run python -m mypy && uv run python -m pytest -q -n 4 {{test_files}}
uv run python -m pytest -q -n 4 -m scan  # the codebase scans: a change anywhere can break them (I-234)
```

## Report
Return your report in the shape of `.claude/templates/agent-report.md`, under 300 words
unless a field needs more. The orchestrator saves it to `.scio/agents/{{issue}}.md` (W-34),
and builds the PR body and the ledger entries from it. Its fields: `design_as_built`,
`files_changed`, `sweep`, `invariant_tests`, `fail_on_old_code`, `existing_tests_changed`,
`edge_decisions`, `open_questions`, `docs_touchpoints_left`, `checks`, `probes`.
