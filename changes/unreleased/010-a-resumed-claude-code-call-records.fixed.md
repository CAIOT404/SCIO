A resumed Claude Code call records its own latency, not the whole session's: `duration_api_ms` is cumulative on a `--resume`, so each summary call repeated its session's time (I-210).
