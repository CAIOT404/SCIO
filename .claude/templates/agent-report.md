<!--
The report a sub-agent returns (issue #122, W-32). The orchestrator saves it to
`.scio/agents/<issue>.md` (W-34); the PR body and the ledger entries are built from it (W-24).
Keep every heading, exactly as written: they are the fields tools read. Write "none" under a
field with nothing to report. Under 300 words unless a field needs more.
-->
# Report: #<issue> <title>

## design_as_built
What you built, in a few sentences: the shape, the names, and where it lives. Say where it
differs from the brief's decided design and why.

## files_changed
One line per file: `path`: what changed. Code, tests and docs alike.

## sweep
The search you ran for the same construct elsewhere (the grep or pattern, verbatim), what it
found, and what you changed at each hit. "none" only when the change fixes no bug.

## invariant_tests
One line per invariant in the brief: the owner's words, quoted, then the test that guards it
by its exact name and file.

## fail_on_old_code
How each new test fails on the old code: the command you ran against the old code and the
failure it printed (the error line, not "it failed").

## existing_tests_changed
Each existing test you changed, and why the change keeps what it checked. "none" when you
changed none. An existing test changed to pass is a red flag: justify it or revert it.

## edge_decisions
One line for each of the brief's four edge decisions, saying what you built: Identity (how
each id, key or file name stays unique across threads, milestones and reruns), Failure (what
the code does when an external call or write fails), Resume (what a rerun, or a record stored
before this change, does), Pins (the tests outside your files that you ran, and their counts).
Where you built something other than the brief's line, say so and why.

## open_questions
Each gap you could not close: the question, what you tried, the evidence, and what would
settle it. Never "owner decision" (W-08): you report the question, the orchestrator decides
who answers it.

## docs_touchpoints_left
Docs that describe your change and that you could not edit (the shared files, or files you
do not own): the path, the line or section, and the text it needs.

## checks
Each check you ran and its counts: `ruff check`, `ruff format --check`, `mypy` (files
checked), and `pytest` (passed, failed, skipped, with the test files you ran).

## probes
Each live call you made (a `claude_probe.py` run, a provider call): the command, the model,
the result and the cost. "none" when you made none.
