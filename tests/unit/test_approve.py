"""`scripts/approve.py` (Workflow v3, WP5): an approval line is stamped by the clock and
checked with ``land.py``'s own parser before it is written to the main checkout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.session_tools_support import clone, git, load, make_hub

AT = datetime(2026, 11, 16, 9, 5, 59, tzinfo=UTC)


def _now() -> datetime:
    return AT


def _repo(tmp_path: Path) -> Path:
    return clone(make_hub(tmp_path), tmp_path / "SCIO")


def _approvals(root: Path) -> Path:
    return root / ".scio" / "approvals.md"


def test_approve_stamps_the_time_and_refs(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    approve = load("approve")
    scio = _repo(tmp_path)
    path = _approvals(scio)
    assert not path.exists()

    code = approve.main(
        ["merge them", "--refs", " issue 140 ,Refs #141", "--root", str(scio)], now=_now
    )

    line = "2026-11-16T09:05Z | merge them | issue 140, Refs #141"
    assert code == 0
    assert capsys.readouterr().out.strip() == line
    assert path.read_bytes() == (line + "\n").encode()  # created, LF ending

    # The clock's zone is converted to UTC, never copied: 01:30 at -08:00 is 09:30Z.
    west = datetime(2026, 11, 16, 1, 30, tzinfo=timezone(timedelta(hours=-8)))
    assert approve.main(["ok", "--refs", "PR 9", "--root", str(scio)], now=lambda: west) == 0
    second = "2026-11-16T09:30Z | ok | PR 9"
    assert path.read_bytes() == f"{line}\n{second}\n".encode()  # appended, the first line kept

    # A file left without a final newline still gets the new line on its own line.
    path.write_bytes(b"# Approvals")
    assert approve.main(["ok", "--refs", "PR 9", "--root", str(scio)], now=_now) == 0
    assert path.read_bytes() == b"# Approvals\n2026-11-16T09:05Z | ok | PR 9\n"

    # A clock with no zone is refused: there is nothing to convert from.
    path.unlink()
    naive = datetime(2026, 11, 16, 1, 30)
    assert approve.main(["ok", "--refs", "PR 9", "--root", str(scio)], now=lambda: naive) == 2
    assert not path.exists()


@pytest.mark.parametrize(
    ("words", "refs"),
    [
        ("merge it", "PR"),  # no number
        ("merge it", "#140"),  # no kind
        ("merge it", "140"),
        ("merge it", "PRs 9 and 10"),
        ("merge it", "PR 9, the docs one"),  # one good ref does not carry a bad one
        ("merge it", "PR 9,"),  # an empty ref
        ("merge it", "PR 9 | PR 10"),  # a field separator inside the refs
        ("merge it", "PR 9\nPR 10"),
        ("merge it", "note"),  # the note marker is not a ref
        ("merge it", ""),
        ("merge it", "   "),
        ("", "PR 9"),
        ("   ", "PR 9"),
        ("merge it | PR 12", "PR 9"),
        ("merge it\nPR 12", "PR 9"),
    ],
)
def test_approve_refuses_refs_land_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], words: str, refs: str
):
    approve = load("approve")
    scio = _repo(tmp_path)

    code = approve.main([words, "--refs", refs, "--root", str(scio)], now=_now)

    out = capsys.readouterr().out
    assert code == 2
    assert out.startswith("approve: refused") and len(out.splitlines()) == 1
    assert not _approvals(scio).exists()  # nothing written, not even an empty file


def test_approve_refuses_an_existing_file_no_change(tmp_path: Path):
    approve = load("approve")
    scio = _repo(tmp_path)
    path = _approvals(scio)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"2026-11-15T09:00Z | merge 9 | PR 9\n")

    assert approve.main(["merge it", "--refs", "PR nine", "--root", str(scio)], now=_now) == 2
    assert approve.main(["words", "--note", "a note too", "--root", str(scio)], now=_now) == 2
    with pytest.raises(SystemExit) as usage:  # neither --refs nor --note: argparse's exit 2
        approve.main(["merge it", "--root", str(scio)], now=_now)

    assert usage.value.code == 2
    assert path.read_bytes() == b"2026-11-15T09:00Z | merge 9 | PR 9\n"


def test_an_approved_line_is_read_by_land(tmp_path: Path):
    approve, land = load("approve"), load("land")
    scio = _repo(tmp_path)

    assert approve.main(["merge 140", "--refs", "issue 140", "--root", str(scio)], now=_now) == 0

    found = land.read_approvals(_approvals(scio).read_text(encoding="utf-8"))
    assert [(a.at, a.words, a.refs) for a in found] == [
        ("2026-11-16T09:05Z", "merge 140", [("issue", 140)])
    ]
    assert land.approval_for(7, "## Summary\n\nCloses #140\n", found) is found[0]
    # Exactly what was named, nothing wider.
    assert land.approval_for(7, "Closes #141", found) is None
    assert land.approval_for(140, "", found) is None


def test_a_note_approves_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    approve, land = load("approve"), load("land")
    scio = _repo(tmp_path)

    # The note's own text may name a PR: only the last field holds refs.
    code = approve.main(["--note", "run WP5 later, after PR 9", "--root", str(scio)], now=_now)

    line = "2026-11-16T09:05Z | run WP5 later, after PR 9 | note"
    assert code == 0
    assert capsys.readouterr().out.strip() == line
    text = _approvals(scio).read_text(encoding="utf-8")
    assert text == line + "\n"
    assert land.read_approvals(text) == []
    assert land.approval_for(9, "Closes #9\nRefs #9", land.read_approvals(text)) is None

    # A note beside an approval leaves the approval as the only one read.
    assert approve.main(["merge 9", "--refs", "PR 9", "--root", str(scio)], now=_now) == 0
    found = land.read_approvals(_approvals(scio).read_text(encoding="utf-8"))
    assert [a.refs for a in found] == [[("pr", 9)]]

    # A note is held to the same words rule.
    before = _approvals(scio).read_bytes()
    assert approve.main(["--note", "later | PR 12", "--root", str(scio)], now=_now) == 2
    assert approve.main(["--note", " ", "--root", str(scio)], now=_now) == 2
    assert _approvals(scio).read_bytes() == before


def test_approve_writes_the_main_checkouts_file_from_a_worktree(tmp_path: Path):
    approve, land = load("approve"), load("land")
    scio = _repo(tmp_path)
    wt = tmp_path / "wt-z"
    git(scio, "worktree", "add", "-q", "-b", "feat/z", str(wt), "origin/main")

    assert approve.main(["merge 9", "--refs", "PR 9", "--root", str(wt)], now=_now) == 0

    assert not _approvals(wt).exists()
    text = _approvals(scio).read_text(encoding="utf-8")
    assert text == "2026-11-16T09:05Z | merge 9 | PR 9\n"
    assert land.approval_for(9, "", land.read_approvals(text)) is not None
