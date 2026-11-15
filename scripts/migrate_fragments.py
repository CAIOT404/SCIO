"""Move the ledger, the guardrails and the unreleased changes into fragments (issue #122).

    uv run python scripts/migrate_fragments.py [--root DIR]

Every pull request appended to the same three places, so every merge conflicted (I-206). This
moves each entry, rule and change into its own file:

- ``docs/ISSUE_LEDGER.md``: each entry goes to ``docs/ledger/I-NNN.md``, its text unchanged
  from its ``### I-NNN · title`` heading to its last line (W-38). The template goes to
  ``docs/ledger/README.md``, and the ledger keeps its front page and a pointer, so links to
  it still work.
- ``## Guardrails`` in ``docs/LESSONS_LEARNED.md``: each rule line goes to
  ``docs/guardrails/NNN.md``, three digits, the line unchanged (W-39). The section keeps its
  heading and its text, and points to the folder.
- ``[Unreleased]`` in ``CHANGELOG.md``: each bullet goes to
  ``changes/unreleased/<n>-<slug>.<added|changed|fixed>.md``, ``<n>`` its place in the
  section (W-40). A fragment holds the bullet's text without its ``- ``, with its later lines
  two spaces less indented; ``release_notes.bullet`` puts both back. The released sections
  are not touched.

It can run again: a fragment that already holds the same text is left as it is, so a second
run changes nothing. A fragment that holds another text is never overwritten. The run then
stops before it writes anything and names the fragment, because a stale copy of an entry
must not replace a newer one (I-206).

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = Path("docs") / "ISSUE_LEDGER.md"
LEDGER_DIR = Path("docs") / "ledger"
LESSONS = Path("docs") / "LESSONS_LEARNED.md"
GUARDRAIL_DIR = Path("docs") / "guardrails"
CHANGELOG = Path("CHANGELOG.md")
CHANGES_DIR = Path("changes") / "unreleased"
CHANGES_README = Path("changes") / "README.md"

ENTRY_RE = re.compile(r"^### (I-\d+) ", re.MULTILINE)
RULE_RE = re.compile(r"^(\d+)\. ")
"""A rule line of ``## Guardrails``: what ``check_docs.guardrail_rules`` reads as a rule."""
GUARDRAILS_HEADING = "\n## Guardrails"
NEXT_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
UNRELEASED_RE = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE)
RELEASE_RE = re.compile(r"^## \[", re.MULTILINE)
KIND_RE = re.compile(r"^### (Added|Changed|Fixed)[ \t]*$")
NUMBER_RE = re.compile(r"^(\d+)-")
"""A change fragment's sequence number, the first digits of its name."""

LEDGER_MARK = "**The entries are files**"
LEDGER_POINTER = (
    f"{LEDGER_MARK}, one per entry, named by its id: `docs/ledger/I-NNN.md` (issue #122, "
    "W-38). Two pull requests that each add an entry no longer conflict here. "
    "[ledger/README.md](ledger/README.md) has the template and how to write an entry; "
    '`uv run python scripts/ledger_new.py --title "<short title>" --where "<file, command or '
    "step>\"` writes the next free id's file. The docs check reads the entries there, and fails "
    "on an entry written into this file. `cat docs/ledger/I-*.md` prints the whole ledger in id "
    "order.\n"
)
GUARDRAIL_MARK = "**The rules are files**"
GUARDRAIL_POINTER = (
    f"{GUARDRAIL_MARK}, one per rule, named by its number: `docs/guardrails/NNN.md`, three "
    "digits (issue #122, W-39). Each holds its one rule line, as it would read in a numbered "
    "list here. [guardrails/](guardrails/) holds them: `cat docs/guardrails/*.md` prints them "
    "in order, and `uv run python scripts/next_ids.py` gives the next number. "
    "`check_guardrails` reads them there, and fails on a rule written into this section.\n"
)
CHANGES_POINTER = (
    "The unreleased changes are files, one per change, in `changes/unreleased/` (issue #122, "
    "W-40); [changes/README.md](changes/README.md) says how to write one. The release cut, "
    "`uv run python scripts/release_notes.py --cut X.Y.Z`, assembles them here into the new "
    "version's section and deletes them.\n"
)
FENCE = "```"
LEDGER_README = "\n".join(
    [
        "# The issue ledger's entries",
        "",
        "One file per entry, named by its id: `I-NNN.md` (issue #122, W-38). "
        "[The ledger's front page](../ISSUE_LEDGER.md) says what the ledger is for. Each file "
        "holds exactly one entry: it starts with its `### I-NNN · <short title>` heading, and "
        "its `- Resolved: … · Status: …` line closes it. Two pull requests that each add an "
        "entry never conflict.",
        "",
        "## Write an entry",
        "",
        "Write it the moment the problem appears, before the gate that carries it starts:",
        "",
        f"{FENCE}sh",
        'uv run python scripts/ledger_new.py --title "<short title>" '
        '--where "<file, command or step>"',
        FENCE,
        "",
        "It takes the next free id from every branch and worktree (`scripts/next_ids.py`), "
        "writes the file with every field `TODO` and the status line "
        "`- Resolved: - · Status: open`, and prints the id. Fill in the fields as the work "
        "goes. When the problem is resolved, set the date and the status. The docs check fails "
        "on a `TODO` field left in an entry that is no longer open, and on an entry without "
        "its status line (I-204).",
        "",
        "## Template",
        "",
        FENCE,
        "### I-NNN · <short title>",
        "- Found: <date> · <milestone> · <where: file, command, step>",
        "- Symptom: <what was observed, exact error text if short>",
        "- Root cause: <the actual mistake, not the symptom>",
        "- Fix: <what changed, commit or PR>",
        "- Sweep: <pattern searched, scope, instances found and fixed>",
        "- Guard: <the test, symbol, docs check or hook that fails if it recurs; its guardrail>",
        "- Resolved: <date> · Status: open | resolved | wontfix",
        FENCE,
        "",
        "Statuses: `open`, `resolved`, `wontfix` (with reason). Every resolved entry from I-104 "
        "on is cited by a guardrail in [../guardrails/](../guardrails/) (`check_guardrails`).",
        "",
        "## Read the ledger",
        "",
        "`cat docs/ledger/I-*.md` prints every entry in id order.",
        "",
    ]
)
CHANGES_README_TEXT = "\n".join(
    [
        "# Unreleased changes",
        "",
        "Each change a pull request makes is one file in `changes/unreleased/` (issue #122, "
        "W-40), so two pull requests never conflict on CHANGELOG.md. The release cut assembles "
        "them into CHANGELOG.md.",
        "",
        "## The name",
        "",
        "`<n>-<slug>.<kind>.md`, for example `014-ledger-fragments.changed.md`:",
        "",
        "- `<n>`: the change's place among the changes of its kind. Take the number after the "
        "highest in the folder, zero-padded to three digits. Two pull requests that take the "
        "same number do not conflict: their slugs differ, and the slug breaks the tie.",
        "- `<slug>`: a few lowercase words joined by hyphens.",
        "- `<kind>`: `added`, `changed` or `fixed`, the section the change is listed under.",
        "",
        "## The text",
        "",
        "The bullet's text, without its leading `- `. A sub-list or a second paragraph starts "
        "at the left margin; the cut indents it under the bullet. Write links as they will read "
        "in CHANGELOG.md, from the repository's root (`docs/RECOVERY.md`, not "
        "`../../docs/RECOVERY.md`): the docs check reads them from there.",
        "",
        "## The release cut",
        "",
        "`uv run python scripts/release_notes.py --cut X.Y.Z` writes the new "
        "`## [X.Y.Z] - <date>` section right below `[Unreleased]`: `### Added`, `### Changed` "
        "and `### Fixed`, each in number order. Then it deletes the fragments. A released "
        "section is never edited after its tag (`check_released_section_frozen`, I-089).",
        "",
    ]
)


class MigrationError(Exception):
    """The old files cannot be moved without losing or overwriting text."""


def _read(path: Path) -> str:
    """A file's exact text: no newline translation, so a comparison is byte for byte."""
    return path.read_bytes().decode("utf-8")


def ledger_entries(text: str) -> list[tuple[str, str]]:
    """``(id, entry)`` for each entry of an old ledger, in file order. An entry runs from its
    heading to the next one; the blank lines between entries are not part of it."""
    heads = list(ENTRY_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), text[m.start() : end].rstrip("\n") + "\n"))
    return out


def ledger_short_form(text: str) -> str:
    """The ledger's front page: its text above the template, the first entry or the pointer,
    then the pointer."""
    cut = len(text)
    for marker in ("\nTemplate:", "\n" + LEDGER_MARK, "\n---\n"):
        i = text.find(marker)
        if i != -1:
            cut = min(cut, i)
    first = ENTRY_RE.search(text)
    if first:
        cut = min(cut, first.start())
    return text[:cut].rstrip("\n") + "\n\n" + LEDGER_POINTER


def guardrail_span(lessons: str) -> tuple[int, int] | None:
    """Where the body of ``## Guardrails`` starts and ends: after its heading line, up to the
    next ``## `` heading."""
    i = lessons.find(GUARDRAILS_HEADING)
    if i == -1:
        return None
    start = lessons.index("\n", i + 1) + 1
    nxt = NEXT_SECTION_RE.search(lessons, start)
    return start, nxt.start() if nxt else len(lessons)


def guardrail_lines(lessons: str) -> list[tuple[int, str]]:
    """``(number, rule line)`` for each rule of ``## Guardrails``."""
    span = guardrail_span(lessons)
    if span is None:
        return []
    out = []
    for ln in lessons[span[0] : span[1]].split("\n"):
        m = RULE_RE.match(ln)
        if m:
            out.append((int(m.group(1)), ln))
    return out


def lessons_short_form(lessons: str) -> str:
    """LESSONS_LEARNED.md with the rule lines of ``## Guardrails`` replaced by the pointer;
    the heading and the text around the rules stay. A section that already has the pointer
    (a rule appended the old way after the migration) loses the rule and keeps its one
    pointer, with no blank line left doubled."""
    span = guardrail_span(lessons)
    if span is None:
        return lessons
    start, end = span
    section = lessons[start:end]
    kept: list[str] = []
    placed = GUARDRAIL_MARK in section
    dropped = False
    for ln in section.split("\n"):
        if RULE_RE.match(ln):
            if not placed:
                kept.append(GUARDRAIL_POINTER.rstrip("\n"))
                placed = True
            else:
                dropped = True
            continue
        if dropped and not ln.strip() and kept and not kept[-1].strip():
            dropped = False
            continue  # the blank line after a dropped rule, next to one already kept
        dropped = False
        kept.append(ln)
    body = "\n".join(kept)
    if not placed:
        body = body.rstrip("\n") + "\n\n" + GUARDRAIL_POINTER + "\n"
    return lessons[:start] + body + lessons[end:]


def unreleased_span(changelog: str) -> tuple[int, int] | None:
    """Where the body of ``## [Unreleased]`` starts and ends."""
    head = UNRELEASED_RE.search(changelog)
    if head is None:
        return None
    nxt = RELEASE_RE.search(changelog, head.end())
    return head.end(), nxt.start() if nxt else len(changelog)


def unreleased_bullets(body: str) -> list[tuple[str, str]]:
    """``(kind, bullet)`` for each top-level bullet of ``[Unreleased]``, in document order:
    the bullet as it reads, its indented lines and inner blank lines included."""
    out: list[tuple[str, str]] = []
    kind: str | None = None
    current: list[str] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            while current and not current[-1].strip():
                current.pop()
            assert kind is not None
            out.append((kind, "\n".join(current)))
        current = None

    for ln in body.split("\n"):
        heading = KIND_RE.match(ln)
        if heading:
            flush()
            kind = heading.group(1).lower()
        elif ln.startswith("- "):
            flush()
            if kind is None:
                raise MigrationError(f"[Unreleased] holds a change under no ### heading: {ln!r}")
            current = [ln]
        elif current is not None and (not ln.strip() or ln.startswith("  ")):
            current.append(ln)
        elif ln.strip() and ln.strip() != CHANGES_POINTER.strip():
            raise MigrationError(f"[Unreleased] holds a line that is in no change: {ln!r}")
    flush()
    return out


def fragment_text(bullet: str) -> str:
    """A bullet's text as its fragment holds it: no ``- ``, later lines two spaces less
    indented, blank lines empty."""
    lines = bullet.split("\n")
    rest = []
    for ln in lines[1:]:
        if not ln.strip():
            rest.append("")
        elif ln.startswith("  "):
            rest.append(ln[2:])
        else:
            raise MigrationError(f"a change's line is not indented under its bullet: {ln!r}")
    return "\n".join([lines[0][2:], *rest]) + "\n"


def slug(text: str, words: int = 6) -> str:
    """A fragment's slug: the first words of its first line, lowercase, joined by hyphens."""
    found = re.findall(r"[a-z0-9]+", text.split("\n", 1)[0].lower())
    return "-".join(found[:words])[:48].strip("-") or "change"


def changelog_short_form(changelog: str) -> str:
    """CHANGELOG.md with the body of ``[Unreleased]`` replaced by the pointer; the released
    sections are not touched."""
    span = unreleased_span(changelog)
    if span is None:
        return changelog
    start, end = span
    return changelog[:start] + "\n" + CHANGES_POINTER + "\n" + changelog[end:]


def plan(root: Path) -> tuple[dict[Path, str], list[str]]:
    """The files to write (path -> text), and the fragments that already hold another text."""
    writes: dict[Path, str] = {}
    conflicts: list[str] = []

    def fragment(rel: Path, text: str, what: str) -> None:
        path = root / rel
        if path in writes and writes[path] != text:
            conflicts.append(f"{rel.as_posix()}: the old file holds two texts for {what}")
        elif path.is_file() and _read(path) != text:
            conflicts.append(
                f"{rel.as_posix()}: already holds another text than {what} in the old file; "
                "settle which is right, then run again"
            )
        elif not path.is_file():
            writes[path] = text

    def rewrite(rel: Path, text: str) -> None:
        path = root / rel
        if not path.is_file() or _read(path) != text:
            writes[path] = text

    def readme(rel: Path, text: str) -> None:
        if not (root / rel).is_file():
            writes[root / rel] = text

    if (root / LEDGER).is_file():
        ledger = _read(root / LEDGER)
        for lid, entry in ledger_entries(ledger):
            fragment(LEDGER_DIR / f"{lid}.md", entry, f"the entry {lid}")
        readme(LEDGER_DIR / "README.md", LEDGER_README)
        rewrite(LEDGER, ledger_short_form(ledger))

    if (root / LESSONS).is_file():
        lessons = _read(root / LESSONS)
        if guardrail_span(lessons) is not None:
            for n, line in guardrail_lines(lessons):
                fragment(GUARDRAIL_DIR / f"{n:03d}.md", line + "\n", f"rule {n}")
            rewrite(LESSONS, lessons_short_form(lessons))

    if (root / CHANGELOG).is_file():
        changelog = _read(root / CHANGELOG)
        span = unreleased_span(changelog)
        if span is not None:
            folder = root / CHANGES_DIR
            names = [p.name for p in folder.glob("*.md")] if folder.is_dir() else []
            present = {_read(folder / name) for name in names}
            numbers = [int(m.group(1)) for name in names if (m := NUMBER_RE.match(name))]
            n = max(numbers, default=0)  # a late change goes after the ones already there
            for i, (kind, bullet) in enumerate(unreleased_bullets(changelog[span[0] : span[1]])):
                text = fragment_text(bullet)
                if text not in present:
                    n += 1
                    name = f"{n:03d}-{slug(text)}.{kind}.md"
                    fragment(CHANGES_DIR / name, text, f"the [Unreleased] change {i + 1}")
            readme(CHANGES_README, CHANGES_README_TEXT)
            rewrite(CHANGELOG, changelog_short_form(changelog))
    return writes, conflicts


def migrate(root: Path = ROOT) -> list[str]:
    """Move the three files' entries, rules and changes into fragments under ``root``;
    returns the files written, relative to ``root`` (none on a rerun). Raises
    :class:`MigrationError`, having written nothing, when a fragment holds another text."""
    writes, conflicts = plan(root)
    if conflicts:
        raise MigrationError("\n".join(conflicts))
    for path, text in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # LF exactly, on every OS
    return sorted(p.relative_to(root).as_posix() for p in writes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="the repository's root")
    args = ap.parse_args(argv)
    try:
        written = migrate(args.root)
    except MigrationError as exc:
        print(f"migrate_fragments: nothing written:\n{exc}", file=sys.stderr)
        return 1
    for rel in written:
        print(rel)
    print(f"migrate_fragments: {len(written)} file(s) written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
