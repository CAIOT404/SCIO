# Dev helpers

Small scripts for inspecting what SCIO and Claude Code did. They are not part of the gate,
the framework or CI. Each has a `main(argv)` and a docstring that says how to run it.

| Script | What it is for |
|---|---|
| `read_stream.py` | Summarize a `claude -p --output-format stream-json` file: each tool use and its input, each tool result (ok or error), the final `result` line, and its permission denials. Read it first when a worker turn stalled or a probe surprised you. |
| `ledger_pairs.py` | Print each call in a feature cost ledger (`<work>/ledger/<thread>.jsonl`): time, role, usage, dollars, latency and request id. A Claude Code worker turn is a pair of calls on one session; a call that repeats its session's previous usage is marked `REPEAT`. |
| `claude_probe.py` | Run one `claude -p` probe with exactly the flags `ClaudeCodeWorker` passes (it imports `worker_argv` and `resume_argv` from `scio/loop/claude_code_worker.py`). It saves the stream, its `read_stream` summary and a JSON record of the argv, exit, session id and timings. |

Run them with `uv run python scripts/dev/<script>.py --help`.

## The rule: inspection scripts live in files

An inspection script lives here or in the session's scratchpad, never inline in a shell
command (guardrail 17, I-176; issue #122, W-05 and W-30). A `python -c` script or a heredoc
that holds a backslash is mangled before Python sees it, and the push-gate hook refuses it.
Write the script with the Edit or Write tool, run it by path, and move it here when it is
worth keeping.

## Probing Claude Code

A question about Claude Code's behaviour is answered by a probe, never from memory
(issue #122, W-31):

- Write the prompt to a file, then run
  `uv run python scripts/dev/claude_probe.py --prompt-file prompt.md --tools coder`.
- The model defaults to the `router` role's model (Haiku); `--model` takes a role or a model id.
- `--cwd` defaults to a new temporary directory outside the repository, so a probe never edits
  SCIO.
- `claude` always runs without `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDECODE`,
  so the login bills and print mode starts inside a Claude Code session.
- `--resume <session>` continues a probe's session with the same flags.
- The output lands in `.scio/probes/<UTC stamp>/` unless `--out` says otherwise.
- A probe is a live call that costs real usage. Tests never run one; they pass a fake runner
  to `main(argv, runner=...)`.
