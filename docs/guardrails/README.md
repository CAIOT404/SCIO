# The guardrails

Every bug and every correction from the owner becomes one Always/Never rule, with the guard that fails when it is broken ([LESSONS_LEARNED.md](../LESSONS_LEARNED.md), the `## Guardrails` section). Each rule is its own file here, named by its number: `NNN.md`, three digits (issue #122, W-39). `uv run python scripts/next_ids.py` gives the next number.

**Read the active rules:** `uv run python scripts/check_docs.py --active` prints every one, the ones a session must read, in number order. [ACTIVE.md](ACTIVE.md) holds those the last review read, up to its cut-off line `Reviewed through rule <N> on <date>.`. A rule added after the review is active until the next one, and `--active` lists it with the rest.

## A rule file

Line one is the rule, as it would read in a numbered list: `N. **Always** ... (I-NNN). Guard: ...`. An active rule has nothing else. After a review, a rule that is no longer active gets a second line, its status, in one of two forms:

- `Status: merged into <n>`: rule `n` now says what this rule said, as one of its clauses. Rule `n` must be active, and it must cite every ledger id this rule cites.
- ``Status: retired: `<kind>:<name>` enforces it``: the guard fully enforces the rule, so no session needs to read it. The guard is a `hook:`, `check:`, `test:` or `symbol:`, never `manual:`, and it must exist. Several guards are written `` `a`, `b` enforce it``.

A rule's first line stays when it is merged or retired (a review may correct a sentence that no longer holds), and its guards are still checked. Numbers are never reused or renumbered, and no rule file is deleted, so a code comment that names "guardrail 30" still leads to it, and from there to the rule that holds it now.

`scripts/check_docs.py` (`check_guardrails`) fails when:

- a resolved ledger entry from I-104 on is cited by no active or retired rule. A merged rule covers its ids only through its target;
- a guard a rule names does not exist, on any rule, whatever its status;
- a merge target is missing, is the rule itself, or is not active, or it does not cite a merged rule's ledger ids;
- a retired rule's guard does not exist, or is `manual:`;
- a status line has any other form, or a file holds any other line.

`check_active_guardrails` fails when [ACTIVE.md](ACTIVE.md) does not match rules 1 to its cut-off: one of them changed, merged or retired since the review, or the file was edited by hand. A pull request that adds a rule changes nothing there, so two of them never conflict on it (W-38), and `main` is never left red between their merges. A review runs `uv run python scripts/check_docs.py --write-active`, which reads every rule there is, takes the highest number as the new cut-off and today (UTC) as its date, and commits the result.

## The review

The owner, issue #141: "Run a quarterly guardrail review: merge related rules, and drop any rule whose guard is now structural (a hook or check enforces it, so nobody needs to read it)." Each review reads every active rule (`--active`), those added since the last cut-off included, and the ledger entries they cite where that helps.

- **Merge** rules that say related things into one active rule. The target keeps each merged rule's concrete Always or Never instruction as a clause of its own, and cites all their ids.
- **Retire** a rule only when its guard enforces it fully: a hook that refuses the act on every route, a check that fails, or code that makes the mistake impossible. A guard that enforces a rule only in part leaves it active. This includes a hook that sees only one tool, a hook that lets the command through when it cannot check it, a scan of some forms of the construct, and a test of one function.
- **Keep** the rules that need judgment.

The first review, 2026-11-15 (issue #141), read rules 1 to 100 and took them to 21 active rules: 75 merged into them, and 4 retired.
