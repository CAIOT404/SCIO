"""Print each call in a feature cost ledger: time, role, usage, usd, latency, request id.

Usage: ``uv run python scripts/dev/ledger_pairs.py <ledger.jsonl> [...]``

A ledger is the ``CostEntry`` lines (``scio.harness.accounting``) a run persists to
``<work>/ledger/<thread>.jsonl``, ``.scio/ledger/`` by default; a product build's features
write theirs beside the build's. On the Claude Code backend a worker turn is two accounted
calls on one session, the main run and the ``--resume`` summary, so both carry the session id
as their ``request_id``: the pairs are what this prints. A call whose usage equals the
previous call's on the same session is marked ``REPEAT``: the resume reported the whole
session's totals instead of its own, and the session was counted twice. ``lat`` is each call's
own latency (I-210).

Issue #122 (W-30): inspection scripts live in ``scripts/dev/`` or a scratchpad, never inline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _usage(entry: dict[str, Any]) -> tuple[int, ...]:
    usage = entry.get("usage") or {}
    return tuple(int(usage.get(k, 0) or 0) for k in USAGE_KEYS)


def rows(lines: Iterable[str]) -> list[str]:
    """One line per call, then a total line."""
    out: list[str] = []
    previous: dict[str, tuple[int, ...]] = {}
    usd_total = 0.0
    calls = 0
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        calls += 1
        usd = float(entry.get("usd") or 0.0)
        usd_total += usd
        inp, outp, cache_read, cache_write = _usage(entry)
        request = str(entry.get("request_id") or "")
        repeat = bool(request) and previous.get(request) == (inp, outp, cache_read, cache_write)
        if request:
            previous[request] = (inp, outp, cache_read, cache_write)
        out.append(
            f"{str(entry.get('at', ''))[11:19]} {entry.get('role', '')!s:9} "
            f"in={inp:>6} out={outp:>7} cr={cache_read:>9} cw={cache_write:>7} "
            f"usd={usd:.4f} lat={int(entry.get('latency_ms') or 0):>7} req={request[:8]}"
            + (" REPEAT" if repeat else "")
        )
    out.append(f"total calls={calls} usd={usd_total:.4f}")
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the calls of each ledger file named in ``argv``."""
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print("usage: ledger_pairs.py <ledger.jsonl> [...]", file=sys.stderr)
        return 2
    for path in paths:
        print(f"== {Path(path).name}")
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in rows(text.splitlines()):
            print(line)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
