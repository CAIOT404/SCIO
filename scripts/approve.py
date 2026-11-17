"""Record the owner's approval in ``.scio/approvals.md``, stamped by the clock (Workflow v3,
WP5).

    uv run python scripts/approve.py "<the owner's words>" --refs "issue 140, Refs #141"
    uv run python scripts/approve.py --note "<a decision that approves no PR>"

It appends one line ``<UTC time> | <the owner's words> | <refs>`` to the main checkout's
``.scio/approvals.md`` (found as ``scripts/land.py`` finds it, also from a worktree), creates
the file when it is missing, and prints the line. The time is the machine's clock in UTC,
``YYYY-MM-DDTHH:MMZ``, never typed. Each ref is ``PR <n>``, ``issue <n>`` or ``Refs #<n>``.

Nothing is written, and it exits 2 with one line, when: the words are empty; they hold a
``|`` or a newline (nothing is replaced silently); ``--refs`` is empty; or any ref is not one
``land.py`` reads as an approval. The line is checked with ``land.py``'s own parser
(``REF_RE`` and ``read_approvals``) before it is written, so what is recorded is what
``land.py`` will read: the same words, and exactly the refs given.

``--note`` records a line whose last field is the literal ``note``. ``land.py`` reads no ref
there, so the line approves no PR; it keeps a decision ("run WP5 later") beside the approvals.

Running it twice appends two lines: each is its own record. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
NOTE = "note"
"""The last field of a line that approves nothing: not a ref ``land.py`` reads."""
TIME_FORMAT = "%Y-%m-%dT%H:%MZ"


class Refused(Exception):
    """The line is not written; the message says why."""


def _session_state() -> ModuleType:
    """``session_state.py`` beside this file, loaded by path under the key
    ``session_state.sibling`` uses, as ``land.py`` loads it."""
    path = SCRIPTS / "session_state.py"
    key = f"_session_tools_session_state_{abs(hash(str(path)))}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod  # @dataclass looks its module up there (I-174)
        spec.loader.exec_module(mod)
    return sys.modules[key]


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(now: datetime) -> str:
    """``now`` in UTC as ``YYYY-MM-DDTHH:MMZ``. A naive time is refused: it has no zone to
    convert from, which is how a hand-typed time went hours wrong."""
    if now.tzinfo is None:
        raise Refused("the clock gave a time with no zone; it must be timezone-aware")
    return now.astimezone(UTC).strftime(TIME_FORMAT)


def clean_words(words: str, what: str = "the words") -> str:
    """``words`` trimmed, or a refusal: empty, or holding a ``|`` (the field separator) or a
    newline (the line separator). Nothing is replaced."""
    text = words.strip()
    if not text:
        raise Refused(f"{what} are empty")
    if "|" in text:
        raise Refused(f"{what} hold a '|', the field separator: reword them, nothing is replaced")
    if "\n" in text or "\r" in text:
        raise Refused(f"{what} hold a newline: one approval is one line")
    return text


def clean_refs(refs: str, land: ModuleType) -> list[str]:
    """The comma-separated refs, each one ``land.py``'s ``REF_RE`` reads, or a refusal."""
    if not refs.strip():
        raise Refused("the refs are empty: name PR <n>, issue <n> or Refs #<n>")
    found: list[str] = []
    for piece in refs.split(","):
        ref = piece.strip()
        if "\n" in ref or "\r" in ref or not land.REF_RE.match(ref):
            raise Refused(
                f"land.py does not read {ref!r} as an approval: each ref is PR <n>, issue <n> "
                "or Refs #<n>, separated by commas"
            )
        found.append(ref)
    return found


def approval_line(at: str, words: str, refs: str, land: ModuleType) -> str:
    """The approval line, proved against ``land.read_approvals``: it reads back as one
    approval with these words and exactly these refs."""
    said = clean_words(words)
    pieces = clean_refs(refs, land)
    line = f"{at} | {said} | {', '.join(pieces)}"
    read = land.read_approvals(line + "\n")
    wanted = [(m.group(1).lower(), int(m.group(2))) for m in map(land.REF_RE.match, pieces) if m]
    if len(read) != 1 or read[0].refs != wanted or read[0].words != said or read[0].at != at:
        raise Refused(f"land.py does not read the line back as written: {line!r}")
    return line


def note_line(at: str, text: str, land: ModuleType) -> str:
    """A line that approves nothing: ``land.read_approvals`` finds no approval in it."""
    line = f"{at} | {clean_words(text, 'the note')} | {NOTE}"
    if land.read_approvals(line + "\n"):
        raise Refused(f"land.py would read the note as an approval: {line!r}")
    return line


def append_line(path: Path, line: str) -> None:
    """Append ``line`` with an LF ending, on a line of its own, creating the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = b""
    lead = "" if not old or old.endswith(b"\n") else "\n"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{lead}{line}\n")


def main(
    argv: list[str] | None = None,
    runner: Any = None,
    now: Callable[[], datetime] = utc_now,
) -> int:
    ss = _session_state()
    ss.console()
    land = ss.sibling("land")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("words", nargs="?", help="the owner's words, as said")
    kind = ap.add_mutually_exclusive_group(required=True)
    kind.add_argument("--refs", help='what they approve: "PR 9, issue 140, Refs #141"')
    kind.add_argument("--note", help="a decision that approves no PR, recorded instead")
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    try:
        at = stamp(now())
        if args.note is not None:
            if args.words is not None:
                raise Refused("give the words with --refs, or the text with --note, not both")
            line = note_line(at, args.note, land)
        else:
            line = approval_line(at, args.words or "", args.refs, land)
        path = ss.main_checkout(args.root, runner or ss.Runner()) / land.APPROVALS
        append_line(path, line)
    except Refused as why:
        print(f"approve: refused, nothing written: {why}", flush=True)
        return 2
    except OSError as exc:
        print(f"approve: could not write the approvals file: {exc}", flush=True)
        return 1
    print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
