"""Summarize a ``claude -p --output-format stream-json`` file, one line per event that matters.

Usage: ``uv run python scripts/dev/read_stream.py <stream.jsonl> [...]``

Each tool use (name and input), each tool result (ok or error, and its start), the final
``result`` line (subtype, turns, cost, session id and text) and its permission denials. A
denial is how Claude Code reports a tool call that the permission flags refused, so it is the
first thing to read when a worker turn stalled. Subagent traffic is marked with ``sub``.
Lines that are not JSON (``Waiting for background work``, stderr mixed in) are skipped.

Issue #122 (W-30): inspection scripts live in ``scripts/dev/`` or a scratchpad, never inline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

TOOL_INPUT_CHARS = 140
RESULT_CHARS = 180
FINAL_CHARS = 240
DENIALS_CHARS = 400


def _flat(text: str, limit: int) -> str:
    return text[:limit].replace("\n", " ")


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content or "")


def summarize(lines: Iterable[str]) -> list[str]:
    """The summary lines of a stream-json transcript."""
    out: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        sub = "sub " if event.get("parent_tool_use_id") else ""
        content = (event.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    args = _flat(json.dumps(block.get("input")), TOOL_INPUT_CHARS)
                    out.append(f"{sub}TOOL_USE {block.get('name')} {args}")
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    status = "error" if block.get("is_error") else "ok"
                    text = _flat(_result_text(block.get("content")), RESULT_CHARS)
                    out.append(f"{sub}RESULT {status} {text}")
        elif kind == "result":
            final = _flat(str(event.get("result")), FINAL_CHARS)
            out.append(
                f"FINAL {event.get('subtype')} turns={event.get('num_turns')} "
                f"usd={event.get('total_cost_usd')} session={event.get('session_id')} {final}"
            )
            denials = _flat(json.dumps(event.get("permission_denials") or []), DENIALS_CHARS)
            out.append(f"DENIALS {denials}")
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the summary of each stream-json file named in ``argv``."""
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print("usage: read_stream.py <stream.jsonl> [...]", file=sys.stderr)
        return 2
    for path in paths:
        if len(paths) > 1:
            print(f"== {path}")
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in summarize(text.splitlines()):
            print(line)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a model's text is not cp1252
    sys.exit(main())
