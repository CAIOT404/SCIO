The gate is quick by default (issue #122, W-11 to W-19):
- It runs lint, types, the tests of the paths changed since the last pass, the evals only when code changed, and the docs check. A Markdown-only change runs only the docs steps. `--full` runs what CI runs.
- A pass stamps its tree in `.scio/gate/last-pass.json`, and a tree that already passed is not gated again. A leftover `RUNNING` marker shows a gate that died. One gate runs at a time on the machine.
- The push gate refuses a head whose tree no pass stamped. Tags, deletions, gated merges, docs stamps, `SCIO_ACK_GATE`, the recovery repair worktree and other repositories pass, and an error in the check allows the push.
- Every refusal is logged outside the repository, to `hook-refusals.log`. A refused inline script is told which scratch file to write instead.
