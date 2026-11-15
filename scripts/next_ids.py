"""The next ledger id and guardrail number, from every ref and worktree (issue #122, W-21).

    uv run python scripts/next_ids.py [--root DIR]

A handoff once said "next I-205" while I-205 was already taken on another branch. The next id
is never remembered: it is the maximum in use anywhere, plus one. "Anywhere" is:

- ``docs/ISSUE_LEDGER.md`` and ``docs/LESSONS_LEARNED.md`` at every local branch and every
  ``origin/*`` branch, read with one ``git grep`` over all their commits;
- the same files in every worktree's working copy, committed or not;
- the fragments, once they exist: ``docs/ledger/I-NNN*.md`` (the id is in the file name) and
  ``docs/guardrails/*.md`` (the number is the name's first digits, or a rule line inside).

A ledger id counts only from an entry heading (``### I-NNN ``), never from a mention in a
body. A guardrail counts only from a numbered ``**Always**``/``**Never**`` rule inside
``## Guardrails``: the retrospectives have numbered lists of their own.

Standard library only; it runs from any checkout and from the session tools.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
LEDGER = "docs/ISSUE_LEDGER.md"
LESSONS = "docs/LESSONS_LEARNED.md"
LEDGER_DIR = "docs/ledger"
GUARDRAIL_DIR = "docs/guardrails"

LEDGER_HEAD_RE = re.compile(r"^### I-(\d+)\b", re.MULTILINE)
RULE_RE = re.compile(r"^(\d+)\. \*\*(?:Always|Never)\*\*", re.MULTILINE)
LEDGER_FRAGMENT_RE = re.compile(r"^I-(\d+)")
DIGITS_RE = re.compile(r"(\d+)")
GREP_PATTERNS = ("^## ", "^### I-[0-9]+", r"^[0-9]+\. \*\*(Always|Never)\*\*")
"""What ``git grep`` keeps of the two files: the section headings (to find ``## Guardrails``),
the ledger's entry headings and the rule lines."""
SHAS_PER_GREP = 200
"""Commits per ``git grep`` call; keeps the command line far below Windows' 32 KB limit."""


class Runs(Protocol):
    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        *,
        input: str | None = None,
        timeout: float | None = 120.0,
    ) -> subprocess.CompletedProcess[str]: ...


class GitRunner:
    """The real runner. Tests pass their own; ``session_state.Runner`` fits too."""

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        *,
        input: str | None = None,
        timeout: float | None = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                cwd=cwd,
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def ledger_max(text: str) -> int:
    """The highest ``### I-NNN`` entry heading in a ledger text; 0 when there is none."""
    return max((int(n) for n in LEDGER_HEAD_RE.findall(text)), default=0)


def guardrail_max(lessons: str) -> int:
    """The highest rule number inside ``## Guardrails`` of LESSONS_LEARNED.md; 0 if none."""
    text = "\n" + lessons
    if "\n## Guardrails" not in text:
        return 0
    section = text.split("\n## Guardrails", 1)[1].split("\n## ", 1)[0]
    return max((int(n) for n in RULE_RE.findall(section)), default=0)


def ledger_fragment_id(name: str) -> int:
    """``I-213.md`` or ``I-213-some-slug.md`` gives 213; anything else 0."""
    m = LEDGER_FRAGMENT_RE.match(name)
    return int(m.group(1)) if m else 0


def guardrail_fragment_number(name: str, text: str = "") -> int:
    """A guardrail fragment's number: the first digits of its name (``089-slug.md``,
    ``G-089.md``), or else the rule line inside it. The fragments' naming is not settled yet
    (W-39), so both are read."""
    if name.lower() == "readme.md":
        return 0
    m = DIGITS_RE.search(Path(name).stem)
    if m:
        return int(m.group(1))
    return max((int(n) for n in RULE_RE.findall(text)), default=0)


def _ref_commits(root: Path, runner: Runs) -> list[str]:
    """The commits at every local branch and every ``origin/*`` branch, each once."""
    proc = runner.run(
        [
            "git",
            "-C",
            str(root),
            "for-each-ref",
            "--format=%(objectname)",
            "refs/heads",
            "refs/remotes/origin",
        ]
    )
    if proc.returncode != 0:
        return []
    return list(dict.fromkeys(ln.strip() for ln in proc.stdout.splitlines() if ln.strip()))


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _grep(root: Path, runner: Runs, args: list[str]) -> str:
    """``git grep`` output; exit 1 (nothing matched) is an empty answer, not a failure."""
    base = ["git", "-C", str(root), "grep", "--no-color", "--no-line-number", "--no-column", "-I"]
    proc = runner.run([*base, *args])
    return proc.stdout if proc.returncode in (0, 1) else ""


def _from_refs(root: Path, runner: Runs) -> tuple[int, int]:
    """The maxima over the committed files at every branch."""
    ledger = guard = 0
    commits = _ref_commits(root, runner)
    for chunk in _chunks(commits, SHAS_PER_GREP):
        patterns = [arg for p in GREP_PATTERNS for arg in ("-e", p)]
        out = _grep(root, runner, ["-E", *patterns, *chunk, "--", LEDGER, LESSONS, GUARDRAIL_DIR])
        lines: dict[tuple[str, str], list[str]] = {}
        for line in out.splitlines():
            parts = line.split(":", 2)  # <commit>:<path>:<text>; paths hold no colon
            if len(parts) == 3:
                lines.setdefault((parts[0], parts[1]), []).append(parts[2])
        for (_commit, path), kept in lines.items():
            text = "\n".join(kept)
            if path == LEDGER:
                ledger = max(ledger, ledger_max(text))
            elif path == LESSONS:
                guard = max(guard, guardrail_max(text))
            elif path.startswith(GUARDRAIL_DIR + "/"):
                guard = max(guard, guardrail_fragment_number(Path(path).name, text))
        names = _grep(root, runner, ["-l", "-e", "", *chunk, "--", LEDGER_DIR, GUARDRAIL_DIR])
        for line in names.splitlines():
            path = line.split(":", 1)[-1]
            name = Path(path).name
            if path.startswith(LEDGER_DIR + "/"):
                ledger = max(ledger, ledger_fragment_id(name))
            elif path.startswith(GUARDRAIL_DIR + "/"):
                guard = max(guard, guardrail_fragment_number(name))
    return ledger, guard


def worktree_paths(root: Path, runner: Runs) -> list[Path]:
    """Every worktree of the repository at ``root`` that exists on disk, the main one first."""
    proc = runner.run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        return [root]
    paths = [
        Path(ln[len("worktree ") :])
        for ln in proc.stdout.splitlines()
        if ln.startswith("worktree ")
    ]
    return [p for p in paths if p.is_dir()] or [root]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _from_worktree(wt: Path) -> tuple[int, int]:
    """The maxima over one worktree's working files, committed or not."""
    ledger = ledger_max(_read(wt / LEDGER))
    guard = guardrail_max(_read(wt / LESSONS))
    for p in sorted((wt / LEDGER_DIR).glob("I-*.md")):
        ledger = max(ledger, ledger_fragment_id(p.name))
    for p in sorted((wt / GUARDRAIL_DIR).glob("*.md")):
        guard = max(guard, guardrail_fragment_number(p.name, _read(p)))
    return ledger, guard


def next_ids(root: Path = ROOT, runner: Runs | None = None) -> tuple[str, int]:
    """``(next_ledger, next_guardrail)``, e.g. ``("I-213", 89)``: the maximum in use at any
    local or ``origin/*`` branch or in any worktree's working files, plus one. It reads the
    refs as they are; fetch first for the newest ``origin/*``."""
    runner = runner or GitRunner()
    ledger, guard = _from_refs(root, runner)
    for wt in worktree_paths(root, runner):
        lg, gd = _from_worktree(wt)
        ledger, guard = max(ledger, lg), max(guard, gd)
    return f"I-{ledger + 1:03d}", guard + 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    ledger, guard = next_ids(args.root)
    print(f"next ledger id: {ledger}")
    print(f"next guardrail: {guard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
