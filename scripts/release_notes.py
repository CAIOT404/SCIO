"""Extract one version's section from CHANGELOG.md as GitHub Release notes, or cut a release.

Usage: ``python scripts/release_notes.py 0.1.0`` (prints the section) or
``python scripts/release_notes.py --cut 0.1.0`` (writes a new ``[0.1.0] - <today>`` section
from the change fragments, deletes them, and prints the section).

The unreleased changes are fragments, one file per change, in ``changes/unreleased/`` (issue
#122, W-40): ``<n>-<slug>.<added|changed|fixed>.md``, each holding one bullet's text without its
``- ``. The cut assembles them under ``### Added``, ``### Changed`` and ``### Fixed``, each kind
in its number order, right below ``[Unreleased]``, whose text (a pointer to the folder) stays.
A released section is never edited after its tag (``check_released_section_frozen``, I-089).

Standard library only: ``release.yml`` runs it with ``uv run --no-project``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
CHANGES = ROOT / "changes" / "unreleased"
HEADER_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$", re.MULTILINE
)
UNRELEASED_RE = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE)
KINDS = ("added", "changed", "fixed")
"""The kinds of change, in the order a released section lists them."""
CHANGE_FRAGMENT_RE = re.compile(r"^(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)\.(added|changed|fixed)\.md$")
"""A change fragment's name: ``<n>-<slug>.<kind>.md``. ``<n>`` orders the changes of a kind."""
NOT_FRAGMENTS = {"README.md"}
"""Files the fragment folder may hold that are not changes."""
BULLET_RE = re.compile(r"^- ", re.MULTILINE)


def sections(text: str) -> dict[str, str]:
    """Map version -> body (text under its header, up to the next header)."""
    out: dict[str, str] = {}
    matches = list(HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group("version")] = text[m.end() : end].strip("\n")
    return out


def notes_for(version: str, text: str | None = None) -> str:
    text = text if text is not None else CHANGELOG.read_text(encoding="utf-8")
    body = sections(text).get(version)
    if body is None:
        raise KeyError(f"CHANGELOG.md has no section for {version!r}")
    return body.strip() + "\n"


def change_fragments(folder: Path | None = None) -> list[Path]:
    """The change fragments in ``folder`` (``changes/unreleased/``), in release order: by
    their number, then by name. A file with another name is not read."""
    folder = folder if folder is not None else CHANGES
    if not folder.is_dir():
        return []
    named = [(m, p) for p in folder.iterdir() if (m := CHANGE_FRAGMENT_RE.match(p.name))]
    return [p for m, p in sorted(named, key=lambda mp: (int(mp[0].group(1)), mp[1].name))]


def stray_files(folder: Path | None = None) -> list[Path]:
    """Files in the fragment folder that are not named as change fragments."""
    folder = folder if folder is not None else CHANGES
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if not CHANGE_FRAGMENT_RE.match(p.name)
        and p.name not in NOT_FRAGMENTS
        and not p.name.startswith(".")
    )


def fragment_kind(path: Path) -> str:
    m = CHANGE_FRAGMENT_RE.match(path.name)
    if m is None:
        raise ValueError(f"{path.name} is not a change fragment's name")
    return m.group(3)


def bullet(text: str) -> str:
    """A fragment's text as one CHANGELOG bullet: ``- `` before its first line, and two
    spaces before every later line that is not blank, so a sub-list or a second paragraph
    stays inside the bullet. No line ends with spaces."""
    lines = text.strip("\n").split("\n")
    rest = [f"  {ln}" if ln.strip() else "" for ln in lines[1:]]
    return "\n".join([f"- {lines[0]}", *rest])


def assemble(fragments: Sequence[Path]) -> str:
    """A released section's body from the fragments: ``### Added``, ``### Changed`` and
    ``### Fixed``, each with its bullets in the order given; a kind with none is left out."""
    blocks = []
    for kind in KINDS:
        bullets = [
            bullet(p.read_text(encoding="utf-8")) for p in fragments if fragment_kind(p) == kind
        ]
        if bullets:
            blocks.append(f"### {kind.capitalize()}\n" + "\n".join(bullets) + "\n")
    return "\n".join(blocks)


def cut(version: str, text: str, body: str, today: str | None = None) -> str:
    """``text`` with a new ``## [version] - today`` section holding ``body``, right below
    ``[Unreleased]``. The ``[Unreleased]`` text (the pointer to the fragments) stays."""
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if f"## [{version}]" in text:
        raise ValueError(f"{version} already has a section")
    head = UNRELEASED_RE.search(text)
    if head is None:
        raise ValueError("no [Unreleased] section to cut")
    nxt = HEADER_RE.search(text, head.end())
    start = nxt.start() if nxt else len(text)
    if BULLET_RE.search(text[head.end() : start]):
        raise ValueError(
            "[Unreleased] still holds changes written the old way; run "
            "`uv run python scripts/migrate_fragments.py` first"
        )
    body = body.strip("\n")
    if not body.strip():
        raise ValueError("no change fragments in changes/unreleased/ to release")
    section = f"## [{version}] - {today}\n{body}\n"
    rest = text[start:]
    return text[:start].rstrip("\n") + "\n\n" + section + (f"\n{rest}" if rest else "")


def cut_release(
    version: str, changelog: Path, folder: Path, today: str | None = None
) -> list[Path]:
    """Cut ``version`` from the fragments in ``folder`` into ``changelog``, then delete the
    fragments; returns the deleted files. CHANGELOG.md is written before anything is
    deleted, so a failed write loses no change."""
    stray = stray_files(folder)
    if stray:
        names = ", ".join(p.name for p in stray)
        raise ValueError(f"not change fragments, see changes/README.md: {names}")
    fragments = change_fragments(folder)
    text = cut(version, changelog.read_text(encoding="utf-8"), assemble(fragments), today)
    changelog.write_text(text, encoding="utf-8", newline="\n")
    for p in fragments:
        p.unlink()
    return fragments


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument(
        "--cut",
        action="store_true",
        help="first write this version's section from changes/unreleased/ and delete them",
    )
    args = ap.parse_args(argv)
    version = args.version.lstrip("v")
    if args.cut:
        try:
            cut_release(version, CHANGELOG, CHANGES)
        except ValueError as exc:
            print(f"release_notes: {exc}", file=sys.stderr)
            return 1
    sys.stdout.write(notes_for(version, CHANGELOG.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
