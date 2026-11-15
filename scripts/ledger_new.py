"""Write a new issue ledger entry, the next free id's file (issue #122, W-41).

    uv run python scripts/ledger_new.py --title "<short title>" --where "<file, command or step>"
        [--milestone <milestone or work>] [--root DIR]

It takes the next id from ``scripts/next_ids.py`` (every local and ``origin/*`` branch and
every worktree), writes ``docs/ledger/I-NNN.md`` with every field ``TODO`` and the status
line ``- Resolved: - · Status: open``, and prints the id. An open entry may keep its
``TODO`` fields; the docs check fails on one left in an entry that is closed. An existing
file is never overwritten.

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = Path("docs") / "ledger"
FIELDS = ("Symptom", "Root cause", "Fix", "Sweep", "Guard")
"""The fields after ``Found``, in the template's order (``docs/ledger/README.md``)."""


def _next_ids() -> ModuleType:
    """``scripts/next_ids.py``, loaded by its path (the scripts are not a package)."""
    path = Path(__file__).resolve().parent / "next_ids.py"
    spec = importlib.util.spec_from_file_location("_ledger_new_next_ids", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # a Protocol class resolves its module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def entry(lid: str, title: str, where: str, milestone: str = "TODO", today: str = "") -> str:
    """A new entry's text: its heading, ``Found`` with the date and the place, every other
    field ``TODO``, and its open status line."""
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"### {lid} · {title}", f"- Found: {today} · {milestone} · {where}"]
    lines += [f"- {field}: TODO" for field in FIELDS]
    lines.append("- Resolved: - · Status: open")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--title", required=True, help="the entry's short title")
    ap.add_argument("--where", required=True, help="where it was found: file, command or step")
    ap.add_argument("--milestone", default="TODO", help="the milestone or the work in progress")
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    for name in ("title", "where", "milestone"):
        value = getattr(args, name)
        if not value.strip() or "\n" in value or "\r" in value:
            print(f"ledger_new: --{name} must be one line of text", file=sys.stderr)
            return 2
    lid, _guardrail = _next_ids().next_ids(args.root)
    path = args.root / LEDGER_DIR / f"{lid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = entry(lid, args.title.strip(), args.where.strip(), args.milestone.strip())
    try:
        with path.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except FileExistsError:
        print(f"ledger_new: {path} already exists; run it again", file=sys.stderr)
        return 1
    print(lid)
    print(f"ledger_new: wrote {(LEDGER_DIR / path.name).as_posix()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
