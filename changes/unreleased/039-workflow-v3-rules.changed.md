CLAUDE.md gains the Workflow v3 rules, each from a measured cost of the 2026-11-14 to 11-17 run:
- an approval is written with `scripts/approve.py`, and "did not object" is not an approval;
- a question to the owner never blocks approved work;
- `main` is not merged into a branch before gating (the merge queue tests against `main`);
- shared docs change in one PR per wave;
- a brief states its four edge decisions and passes `brief.py --check` before it is sent;
- a `gh` or `jq` line with a backslash goes in a file.
