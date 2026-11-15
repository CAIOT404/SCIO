# Contributing

These rules apply to human contributors and to SCIO's own agents.

## Workflow
1. **Start from an issue.** Use one of the issue templates (feature, bug, chore).
2. **Branch:** `agent/<issue#>-<slug>` for agent work, `<type>/<issue#>-<slug>` for humans. Never push to `main` directly; never force-push. A ruleset on `main` enforces this: pull request required, the `checks` workflow must pass, no force-pushes, no deletion.
3. **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`). One commit per passing feature.
4. **Pull request:** open it as a draft using the PR template. Link the issue with `Closes #N`. It becomes ready for review only after all checks pass (and, for agent runs, after the Opus gate passes).
5. **Merge:** always approved by a human. Before any post-merge step (deleting a branch, branching from `main`, tagging), confirm on GitHub that the PR is merged: `gh pr view N --json mergedAt` must be non-null, in its own step, not inside an `&&` chain (I-087).
6. **Release after every milestone:** when a milestone PR merges, the next PR is the release: `python scripts/release_notes.py --cut X.Y.Z` (it writes the new section from `changes/unreleased/` and deletes the fragments), the version in `pyproject.toml`, `scio/__init__.py` and `uv.lock`, the README status line; after its merge, tag `vX.Y.Z` on the merge commit and push the tag. `check_release_cadence` fails when two finished milestones wait unreleased (I-086).

## Docs-as-code
Docs change **in the same PR** as the code they describe:
- `README.md` when usage changes.
- `docs/ARCHITECTURE.md` and its Mermaid diagrams when the graph changes.
- `docs/adr/NNNN-title.md` for any architectural decision, replan, or high-impact change.
- A change fragment in `changes/unreleased/`, one file per change ([changes/README.md](changes/README.md)), never `CHANGELOG.md` itself: the release cut assembles the fragments into it (#122, W-40).
- Docstrings for every public API.

CI checks docs: links, runnable examples, architecture diagram vs. the compiled graph, and public-API doc coverage. A docs failure blocks merge just like a test failure.

## Required checks (every PR)
- lint
- type-check
- tests (Fake provider, offline)
- docs checks
- regression eval suite

## The landing page
- `README.md` says where the project stands at every push: its `> Status:` line names the latest released version and the milestone in progress, the milestone table marks what is done, and the ADR list names the newest ADR. `scripts/check_docs.py::check_readme_status` fails the pre-push chain and CI when it is stale (owner's rule, 2026-10-26).

## Tests and the environment
- `Settings` is environment-backed. A test of a `Settings` field constructs `Settings(field=...)` explicitly and never relies on the default: CI exports values (`SCIO_PROVIDER_OVERRIDE=fake`, `SCIO_PRODUCT_SANDBOX_BACKEND=local`) that a bare `Settings()` inherits (I-080).
- A test that depends on a tool being importable asserts where it comes from (`__file__` under the expected environment), not only that it imports; the dev machine's global packages are not the runner's (I-074).
- Run the local gate with `uv run python scripts/gate.py` (issues #88 and #122):
  - By default it is quick: lint, types, the tests of the paths changed since the last pass, the evals only when code changed, then the docs check. A Markdown-only change runs only the docs steps.
  - `--full` runs everything, as CI does. Use it for a release cut, after a red CI, and for a change to the gate or the hook.
  - A pass stamps its tree in `.scio/gate/last-pass.json`, and a tree that already passed is not gated again.
  - One gate runs at a time on the machine.

  You can also run the suite alone with `uv run python -m pytest -n 16`, and the type check with `uv run python -m mypy`: some machines' application-control policies block the venv's `pytest.exe` and `mypy.exe` launchers (I-075, I-146). The suite runs in parallel locally, as in CI (issues #73, #88). Tests must not share files, ports or global state across workers: use `tmp_path`, port 0, and per-test checkpoints. A test that fails only in parallel is rerun alone by its node id to tell a shared-state bug from a real failure, and gets a ledger entry either way.
- Git in tests sees what a CI runner sees: `tests/conftest.py` empties the global config and forbids a guessed identity, so a commit that names no identity fails locally as on CI. SCIO's own commits pass `SCIO_GIT_IDENTITY`; a test helper that commits passes `-c user.name=... -c user.email=...` (I-104).
- Before pushing the next commit to a branch, confirm the previous push's CI finished green (`gh run list --branch <branch> --limit 3 --json headSha,conclusion`); a watcher on the newest run alone misses an older red one (I-104). After a push, `uv run python scripts/watch_ci.py` waits for the pushed head's own run (by SHA) and watches all its jobs to the end (issue #88). For AI sessions the push-gate hook enforces it (`scripts/hooks/push_gate.py`, wired in `.claude/settings.json`): a `git push` is denied while the branch's latest run is red or still running; to push the fix for a red run, prefix the push with `SCIO_ACK_CI=<run id>`. The same hook refuses a bare `git stash`, because parallel sessions share the stash stack. It holds a `git commit` until the changed Python files pass `ruff check` and `ruff format --check` (I-106).

  In this repository it refuses a `git push` whose head's tree no gate pass stamped (#122, W-17). These pass anyway:
  - tags and deletions;
  - a merge commit whose tree equals a gated tree;
  - a docs-only change with a docs stamp;
  - the recovery repair worktree;
  - an explicit `SCIO_ACK_GATE=<reason>`.

  Every refusal is logged outside the repository, to `hook-refusals.log` (W-18). It also refuses two more kinds of command, each with a fix:
  - a heredoc body or a `python -c` script that holds a backslash, and any command with a double backslash, which the Bash tool halves before the shell runs (guardrail 17, I-176). Write such text with the Edit or Write tool, or put a pattern in a file.
  - a check piped into another command without `set -o pipefail` (guardrail 64, I-173). Start the line with `set -o pipefail`, or drop the pipe.

  A second hook, `scripts/hooks/worktree_guard.py`, judges every Edit, Write and NotebookEdit. A session whose working directory is a worktree may not write the main checkout's tree, whose files carry the same names. A path that drops the worktree's segment would otherwise edit the wrong copy (I-226). The refusal names the worktree's copy of the path. Writes outside the repository, in any worktree, and to paths git ignores (`.scio/`) pass. To change the main checkout on purpose, run the session from it.

  On the owner's machine the user settings (`~/.claude/settings.json`) run the same hook in every session, any project: a session started outside the repository loads no project hooks (I-178, guardrail 68). That copy runs from `.scio/live-wt`, so a hook change reaches other sessions when the SCIO session moves live-wt after the merge.
- CI runs `changes`, three `tests` shards, `evals` and `docker-live` side by side (issues #88 and #122):
  - The shards together cover every test file, `tests/live` included (`test_ci_shards_cover_every_test_directory`). Lint, mypy and the docs check run on shard 1. Each shard prints its 30 slowest tests, for rebalancing.
  - `changes` (`scripts/ci_changes.py`) marks a pull request whose diff touches no code path. Only then are the evals skipped, by a job condition, never a workflow `paths:` filter.
  - The required status `checks` always runs. It passes only when `changes` and every shard succeeded, and `evals` succeeded or was skipped for a no-code PR.
  - `merge_group` runs everything. Every job has a Docker daemon, so the docker-live tests also run in `tests` on the plain Python image; a test that needs the built sandbox image skips there only when `SCIO_SANDBOX_DOCKER_IMAGE` is unset (I-105).

## Continuous improvement protocols
These five protocols are mandatory for humans and agents. `CLAUDE.md` restates them for AI sessions.

1. **Issue ledger** ([docs/ledger/](docs/ledger/README.md), one file per entry, `I-NNN.md`). Every problem found while working (failing check, wrong assumption, tooling failure, environment quirk) gets an entry *when it is found*, and the entry is completed *when it is resolved*: symptom, root cause, fix, the sweep performed, status. `uv run python scripts/ledger_new.py --title "…" --where "…"` writes the next id's entry with every field `TODO`; the docs check fails on a `TODO` left in a closed entry (#122, W-38, W-41). A PR that fixed a problem links its ledger ids. One exception: a habit lapse the push-gate hook refused, with nothing getting through, goes to `hook-refusals.log` instead, and the retrospective reports its count per rule (#122, W-18).
2. **Milestone retrospectives** ([docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md)). At the end of every milestone, record what succeeded, what failed, and the concrete actions taken. Read the whole document before starting the next milestone and cite the lessons that shaped the plan.
3. **Systemic bug sweeps.** A bug is never fixed in isolation. After the root cause is known, search the entire codebase for the same construct (state the grep or pattern in the ledger entry), fix every instance, and add a guard that prevents recurrence (a test, a lint rule, a docs check, or a CONTRIBUTING rule). "Patched the symptom" is not a valid resolution.
4. **Always/Never guardrails** ([docs/guardrails/](docs/guardrails/), one file per rule, `NNN.md`; how they work: `## Guardrails` in [docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md)). Every bug and every correction becomes one rule that starts with **Always** or **Never**, cites its id, and names the guard that fails when the rule is broken: a structural fix, a regression test that failed on the old code, a scan of the codebase, a docs check or a hook; `manual` with the reason only when nothing can check it. `scripts/check_docs.py::check_guardrails` enforces both halves: every resolved ledger entry from I-104 on is cited, and every named guard exists.
5. **Bottleneck audit after every pass.** After each slice, milestone or live run, record the slowest steps (minutes), sequential work that could have run in parallel, repeated friction, and one or two concrete improvements (a file change or a ledger id), in the status report and the retrospective.

## Concurrent sessions
- One session (human or agent) per working checkout. A second concurrent session works in its own `git worktree add` directory (ledger I-058).
- Every scripted commit starts with a branch assertion, e.g. `test "$(git branch --show-current)" = "docs/34-product-mode" && git commit ...`, so a checkout switched by someone else fails loudly instead of committing to the wrong branch.

## File hygiene
- Line endings are LF everywhere (`.gitattributes` enforces it). When writing files from Python on Windows, pass `newline="\n"`; ledger entry I-006 explains why.
- Model IDs live only in `configs/models.yaml`; `scripts/check_docs.py` fails on any other occurrence.
