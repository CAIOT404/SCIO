"""The Workflow v2 checklist's status, computed from the tree (issue #122).

    uv run python scripts/workflow_status.py            # every item, done or open
    uv run python scripts/workflow_status.py --session  # the SessionStart hook's JSON
    uv run python scripts/workflow_status.py --check    # malformed items; exit 1 on any

``docs/WORKFLOW_V2.md`` lists the changes from the 2026-11-14 workflow teardown as items
``### W-NN · title``. Each item names its **evidence**, and an item is done exactly when all
of its evidence holds in the tree. Nobody ticks a box, so a box cannot claim work that is
not there, and PRs never edit the checklist to mark progress (it would conflict on every
merge). Evidence tokens, in backticks on the item's ``- Evidence:`` line:

- ``grep:<path>:<literal>``: the file contains the literal text.
- ``file:<path>``: the file exists.
- ``symbol:<path>:<name>``: the Python file defines ``name`` (``def``, ``class``, or an
  assignment at the start of a line).
- ``test:<name>``: a test function ``name`` exists under ``tests/``.
- ``manual:<reason>``: nothing in the tree can show it (a GitHub setting, a file outside
  the repository). The item is done only with a ``- Done: <date> · <evidence>`` line, the one
  edit the checklist takes after it lands, and only for a manual item.

An item may instead say ``- Superseded by: W-NN`` (closed when that item is done) or
``- Dropped: <reason>`` (closed; only on the owner's word). Ids are contiguous from W-01, so
an item cannot vanish without a trace.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "WORKFLOW_V2.md"
ITEM_RE = re.compile(r"^### (W-(\d{2})) · (.+)$", re.MULTILINE)
FIELD_RE = re.compile(r"^- (From|Change|PR|Evidence|Done|Superseded by|Dropped):[ \t]*(.*)$")
"""A field's text may be empty when a list follows it (``- Change:`` and its bullets)."""
TOKEN_RE = re.compile(r"`([a-z]+):([^`]*)`")
KINDS = ("grep", "file", "symbol", "test", "manual")


@dataclass
class Item:
    id: str
    number: int
    title: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def tokens(self) -> list[tuple[str, str]]:
        return TOKEN_RE.findall(self.fields.get("Evidence", ""))


def parse(text: str) -> list[Item]:
    """The checklist's items, in document order."""
    heads = list(ITEM_RE.finditer(text))
    items: list[Item] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end() : end].split("\n## ", 1)[0]  # a "## " section ends an item
        item = Item(id=m.group(1), number=int(m.group(2)), title=m.group(3).strip())
        for line in body.splitlines():
            f = FIELD_RE.match(line)
            if f:
                item.fields[f.group(1)] = f.group(2).strip()
        items.append(item)
    return items


def problems(items: list[Item]) -> list[str]:
    """What makes the checklist malformed; an open item is not a problem."""
    errors: list[str] = []
    ids = {it.id for it in items}
    for expected, it in enumerate(items, start=1):
        if it.number != expected:
            errors.append(
                f"{it.id}: ids must run W-01, W-02, … without gaps (expected W-{expected:02d})"
            )
        for name in ("From", "Change"):
            if name not in it.fields:
                errors.append(f"{it.id}: no '- {name}:' line")
        closers = [k for k in ("Evidence", "Superseded by", "Dropped") if it.fields.get(k)]
        if len(closers) != 1:
            errors.append(f"{it.id}: needs exactly one of Evidence, Superseded by, Dropped")
        if "Evidence" in it.fields:
            if not it.tokens:
                errors.append(f"{it.id}: its Evidence line names no `kind:value` token")
            for kind, value in it.tokens:
                if kind not in KINDS:
                    errors.append(f"{it.id}: unknown evidence kind {kind!r}")
                elif kind in ("grep", "symbol") and ":" not in value:
                    errors.append(f"{it.id}: {kind}: needs <path>:<text>")
                elif not value.strip():
                    errors.append(f"{it.id}: empty {kind}: token")
        manual = any(kind == "manual" for kind, _ in it.tokens)
        if "Done" in it.fields and not manual:
            errors.append(f"{it.id}: a Done line is only for a manual item; its evidence decides")
        target = it.fields.get("Superseded by", "")
        if target and target.split()[0] not in ids:
            errors.append(f"{it.id}: superseded by {target!r}, which is not an item")
    return errors


def holds(kind: str, value: str, root: Path = ROOT) -> bool:
    """Whether one evidence token holds in the tree at ``root``."""
    if kind == "file":
        return (root / value).is_file()
    if kind == "grep":
        path, literal = value.split(":", 1)
        p = root / path
        return p.is_file() and literal in p.read_text(encoding="utf-8", errors="replace")
    if kind == "symbol":
        path, name = value.split(":", 1)
        p = root / path
        if not p.is_file():
            return False
        n = re.escape(name)
        pattern = rf"^\s*(?:async\s+)?(?:def {n}\b|class {n}\b|{n}\s*[:=])"
        return re.search(pattern, p.read_text(encoding="utf-8"), re.MULTILINE) is not None
    if kind == "test":
        pattern = re.compile(rf"^\s*def {re.escape(value)}[\[(]", re.MULTILINE)
        return any(
            pattern.search(p.read_text(encoding="utf-8"))
            for p in (root / "tests").rglob("*.py")
            if "__pycache__" not in p.parts
        )
    return False  # manual: only its Done line closes it


def status(item: Item, by_id: dict[str, Item], root: Path = ROOT, depth: int = 0) -> str:
    """``done``, ``open``, ``superseded`` or ``dropped``."""
    if item.fields.get("Dropped"):
        return "dropped"
    target = item.fields.get("Superseded by", "")
    if target:
        other = by_id.get(target.split()[0])
        if other is None or depth > len(by_id):
            return "open"
        return "superseded" if status(other, by_id, root, depth + 1) == "done" else "open"
    tokens = item.tokens
    if any(kind == "manual" for kind, _ in tokens):
        others = [(k, v) for k, v in tokens if k != "manual"]
        return (
            "done"
            if item.fields.get("Done") and all(holds(k, v, root) for k, v in others)
            else "open"
        )
    return "done" if tokens and all(holds(k, v, root) for k, v in tokens) else "open"


def report(root: Path = ROOT) -> tuple[list[tuple[Item, str]], list[str]]:
    items = parse((root / "docs" / "WORKFLOW_V2.md").read_text(encoding="utf-8"))
    by_id = {it.id: it for it in items}
    return [(it, status(it, by_id, root)) for it in items], problems(items)


CLOSED = ("done", "superseded", "dropped")


def session_text(rows: list[tuple[Item, str]], errors: list[str], limit: int = 8) -> str:
    """The few lines every session starts with until the checklist is complete."""
    closed = sum(1 for _, s in rows if s in CLOSED)
    head = f"Workflow v2 checklist (docs/WORKFLOW_V2.md, issue #122): {closed}/{len(rows)} closed."
    if errors:
        head += (
            f" It is MALFORMED ({len(errors)} problem(s)): run scripts/workflow_status.py --check."
        )
    open_rows = [it for it, s in rows if s not in CLOSED]
    if not open_rows:
        return head + " Every item is closed: close issue #122."
    lines = [
        head,
        "Open items, the next first (pick them up as you work; never tick them by hand):",
    ]
    lines += [f"- {it.id} · {it.title}" for it in open_rows[:limit]]
    if len(open_rows) > limit:
        lines.append(f"- … and {len(open_rows) - limit} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        rows, errors = report()
    except FileNotFoundError:
        rows, errors = [], ["docs/WORKFLOW_V2.md is missing"]
    if "--check" in args:
        for e in errors:
            print(f"workflow-status: {e}")
        return 1 if errors else 0
    if "--session" in args:
        text = session_text(rows, errors)
        closed = sum(1 for _, s in rows if s in CLOSED)
        banner = f"Workflow v2: {closed}/{len(rows)} closed (docs/WORKFLOW_V2.md)"
        print(
            json.dumps(
                {
                    "systemMessage": banner,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": text,
                    },
                }
            )
        )
        return 0
    width = max((len(it.title) for it, _ in rows), default=10)
    for it, s in rows:
        print(f"{it.id}  {s:<10}  {it.title[:width]}")
    closed = sum(1 for _, s in rows if s in CLOSED)
    print(
        f"{closed}/{len(rows)} closed"
        + (f"; {len(errors)} problem(s), see --check" if errors else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
