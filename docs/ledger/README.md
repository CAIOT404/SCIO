# The issue ledger's entries

One file per entry, named by its id: `I-NNN.md` (issue #122, W-38). [The ledger's front page](../ISSUE_LEDGER.md) says what the ledger is for. Each file holds exactly one entry: it starts with its `### I-NNN · <short title>` heading, and its `- Resolved: … · Status: …` line closes it. Two pull requests that each add an entry never conflict.

## Write an entry

Write it the moment the problem appears, before the gate that carries it starts:

```sh
uv run python scripts/ledger_new.py --title "<short title>" --where "<file, command or step>"
```

It takes the next free id from every branch and worktree (`scripts/next_ids.py`), writes the file with every field `TODO` and the status line `- Resolved: - · Status: open`, and prints the id. Fill in the fields as the work goes. When the problem is resolved, set the date and the status. The docs check fails on a `TODO` field left in an entry that is no longer open, and on an entry without its status line (I-204).

## Template

```
### I-NNN · <short title>
- Found: <date> · <milestone> · <where: file, command, step>
- Symptom: <what was observed, exact error text if short>
- Root cause: <the actual mistake, not the symptom>
- Fix: <what changed, commit or PR>
- Sweep: <pattern searched, scope, instances found and fixed>
- Guard: <the test, symbol, docs check or hook that fails if it recurs; its guardrail>
- Resolved: <date> · Status: open | resolved | wontfix
```

Statuses: `open`, `resolved`, `wontfix` (with reason). Every resolved entry from I-104 on is cited by a guardrail in [../guardrails/](../guardrails/) (`check_guardrails`).

## Read the ledger

`cat docs/ledger/I-*.md` prints every entry in id order.
