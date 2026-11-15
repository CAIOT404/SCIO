Session tools (issue #122, W-20 to W-29):
- `scripts/session_state.py` is the state-first inspection in one command; with `--markdown` it gives the handoff's facts, which a `Stop` hook writes to `.scio/session-state.md`.
- `scripts/next_ids.py` reads the next ledger id and guardrail from every branch and worktree.
- `scripts/land.py` takes a PR up to the merge: retarget, update, require a gate stamp, watch CI by commit, mark ready. It prints the merge only for a PR that `.scio/approvals.md` approves. It also has `--body`, `--after-merge` and `--next`.
- `scripts/move_live_wt.py --when-idle` moves live-wt only while no supervised build runs.
