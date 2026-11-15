"""The guardrail review (issue #141, parts 3b and 3c).

The owner: "Run a quarterly guardrail review: merge related rules, and drop any rule whose
guard is now structural (a hook or check enforces it, so nobody needs to read it)." A rule
file ``docs/guardrails/NNN.md`` may carry a second line, its status:

- ``Status: merged into <n>``: rule ``n`` is active and cites every ledger id of this one;
- ``Status: retired: `<kind>:<name>` enforces it``: the guard fully enforces the rule.

An active rule has no status line. Numbers are never reused, and no file is deleted. A review
lists the active rules it read, in number order, in the generated
``docs/guardrails/ACTIVE.md``, under its cut-off line ``Reviewed through rule N on DATE.``;
the docs check fails when that list drifts from rules 1 to N. A rule added after the review
needs no change there, and ``check_docs.py --active`` prints every active rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from tests.session_tools_support import load, write

RESOLVED = "- Resolved: 2026-11-15 · Status: resolved\n"
WRITE_ACTIVE = "uv run python scripts/check_docs.py --write-active"
LIST_ACTIVE = "uv run python scripts/check_docs.py --active"


def rule(n: int, ids: list[str], status: str = "", verb: str = "Always") -> str:
    """A rule file's text: its one rule line, and its status line when it has one."""
    line = f"{n}. **{verb}** keep rule {n} ({', '.join(ids)}). Guard: `manual:` a reason\n"
    return line + (f"{status}\n" if status else "")


def _root(tmp_path: Path, files: dict[str, str]) -> Path:
    """A tree holding a ledger of resolved I-104 to I-107, a test the retired rules can name,
    and ``files``; ``check_docs.ROOT`` points at it."""
    root = tmp_path / "repo"
    ledger = {
        f"docs/ledger/I-{n}.md": f"### I-{n} · entry {n}\n- Fix: x\n{RESOLVED}"
        for n in (104, 105, 106, 107)
    }
    guard = {"tests/test_guard.py": "def test_the_guard_refuses() -> None:\n    pass\n"}
    write(root, {**ledger, **guard, **files})
    return root


@pytest.fixture
def cd() -> ModuleType:
    return load("check_docs")


def test_a_merged_rule_counts_its_ids_through_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Invariant 1, "merge related rules": a merged rule's ledger ids stay covered because
    its target cites them, not because the merged file still names them."""
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105", "I-106", "I-107"]),
            "docs/guardrails/002.md": rule(2, ["I-105"], "Status: merged into 1"),
            "docs/guardrails/003.md": rule(3, ["I-106", "I-107"], "Status: merged into 1"),
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    assert cd.check_guardrails(root) == []
    write(root, {"docs/guardrails/001.md": rule(1, ["I-104", "I-106"])})
    errors = cd.check_guardrails(root)
    assert "docs/guardrails: guardrail 2: merged into 1, which does not cite I-105" in errors
    assert "docs/guardrails: guardrail 3: merged into 1, which does not cite I-107" in errors
    # the merged file's own citation covers nothing: the entry is owed a rule again
    uncited = "docs/guardrails: {} is resolved but no guardrail cites it; add an Always/Never rule"
    assert uncited.format("I-105") in errors
    assert uncited.format("I-107") in errors
    assert uncited.format("I-106") not in errors  # the target still cites it


def test_a_merge_target_must_be_active_and_cite_the_merged_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Invariant 1: a merge points at a rule a session still reads. A target that is itself
    merged or retired, a number with no rule, and a rule merged into itself are refused."""
    retired = "Status: retired: `test:test_the_guard_refuses` enforces it"
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105", "I-106", "I-107"]),
            "docs/guardrails/002.md": rule(2, ["I-104"], retired),
            "docs/guardrails/003.md": rule(3, ["I-105"], "Status: merged into 2"),
            "docs/guardrails/004.md": rule(4, ["I-106"], "Status: merged into 3"),
            "docs/guardrails/005.md": rule(5, ["I-107"], "Status: merged into 99"),
            "docs/guardrails/006.md": rule(6, ["I-104"], "Status: merged into 6"),
            "docs/guardrails/007.md": rule(7, ["I-104", "I-105"], "Status: merged into 1"),
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    errors = [e.removeprefix("docs/guardrails: ") for e in cd.check_guardrails(root)]
    assert "guardrail 3: merged into 2, which is not active; merge into an active rule" in errors
    assert "guardrail 4: merged into 3, which is not active; merge into an active rule" in errors
    assert "guardrail 5: merged into 99, which does not exist" in errors
    assert "guardrail 6: merged into itself" in errors
    assert not any(e.startswith("guardrail 7:") for e in errors), errors  # a good merge
    assert not any("is resolved but no guardrail cites it" in e for e in errors), errors
    write(root, {"docs/guardrails/001.md": rule(1, ["I-104", "I-106", "I-107"])})
    errors = [e.removeprefix("docs/guardrails: ") for e in cd.check_guardrails(root)]
    assert "guardrail 7: merged into 1, which does not cite I-105" in errors


def test_a_retired_rule_must_name_a_guard_that_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Invariant 2, "drop any rule whose guard is now structural": a retired rule names the
    hook, check, test or symbol that enforces it, and that guard must exist, checked as a
    rule's guards are. ``manual:`` enforces nothing, so it never retires a rule."""
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105", "I-106", "I-107"]),
            "docs/guardrails/002.md": rule(
                2, ["I-104"], "Status: retired: `test:test_the_guard_refuses` enforces it"
            ),
            "docs/guardrails/003.md": rule(
                3, ["I-105"], "Status: retired: `check:check_guardrails` enforces it"
            ),
            "docs/guardrails/004.md": rule(
                4,
                ["I-106"],
                "Status: retired: `check:check_guardrails`, `test:test_the_guard_refuses` "
                "enforce it",
            ),
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    assert cd.check_guardrails(root) == []
    write(
        root,
        {
            "docs/guardrails/005.md": rule(
                5, ["I-107"], "Status: retired: `test:test_that_never_was` enforces it"
            ),
            "docs/guardrails/006.md": rule(
                6, ["I-107"], "Status: retired: `check:check_that_never_was` enforces it"
            ),
            "docs/guardrails/007.md": rule(
                7, ["I-107"], "Status: retired: `manual:` nobody forgets it enforces it"
            ),
        },
    )
    errors = [e.removeprefix("docs/guardrails: ") for e in cd.check_guardrails(root)]
    assert "guardrail 5: retired by test:test_that_never_was, which does not exist" in errors
    assert "guardrail 6: retired by check:check_that_never_was, which does not exist" in errors
    assert any(e.startswith("guardrail 7: the status line is malformed") for e in errors), errors
    assert len(errors) == 3, errors


REVIEWED = {
    "docs/guardrails/001.md": rule(1, ["I-104", "I-105"]),
    "docs/guardrails/002.md": rule(2, ["I-105"], "Status: merged into 1"),
    "docs/guardrails/003.md": rule(
        3, ["I-106"], "Status: retired: `check:check_guardrails` enforces it"
    ),
    "docs/guardrails/010.md": rule(10, ["I-107"], verb="Never"),
    "docs/guardrails/README.md": "# Guardrails\n\nHow the rules work.\n",
}
"""A tree a review read through rule 10: 1 and 10 active, 2 merged into 1, 3 retired."""


def _reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType) -> Path:
    """The ``REVIEWED`` tree, with ACTIVE.md written by the review on 2026-11-15."""
    root = _root(tmp_path, REVIEWED)
    monkeypatch.setattr(cd, "ROOT", root)
    cd.write_active_guardrails(root, "2026-11-15")
    return root


def test_the_active_rule_list_matches_the_rule_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Invariant 3, "nobody needs to read it": the active rules a review read are the
    reading list, in one generated file that names the review's cut-off. The docs check
    fails when ACTIVE.md is missing or drifts from rules 1 to that cut-off, and names the
    command that writes it."""
    root = _root(tmp_path, REVIEWED)
    monkeypatch.setattr(cd, "ROOT", root)
    active = root / "docs" / "guardrails" / "ACTIVE.md"
    missing = cd.check_active_guardrails(root)
    assert len(missing) == 1 and WRITE_ACTIVE in missing[0], missing

    assert cd.main(["--write-active"]) == 0  # the review reads every rule: cut-off 10
    text = active.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")  # as ledger_new.py dates an entry
    assert f"\nReviewed through rule 10 on {today}.\n" in text
    assert cd.check_active_guardrails(root) == []
    assert cd.check_guardrails(root) == []  # ACTIVE.md and README.md are not rule files
    assert "keep rule 1 " in text and "keep rule 10 " in text
    assert text.index("keep rule 1 ") < text.index("keep rule 10 ")  # number order
    assert "keep rule 2 " not in text and "keep rule 3 " not in text  # merged, retired
    assert "Merged here: 2." in text
    assert f"Rules after 10 are active until the next review: `{LIST_ACTIVE}`" in text
    assert active.read_bytes().count(b"\r") == 0  # LF

    # a rule the review read changes and ACTIVE.md is left behind: drift
    write(root, {"docs/guardrails/010.md": rule(10, ["I-107"], verb="Always")})
    drift = cd.check_active_guardrails(root)
    assert len(drift) == 1 and "docs/guardrails/ACTIVE.md" in drift[0] and WRITE_ACTIVE in drift[0]
    # one the review read is merged without writing the list again: drift
    cd.write_active_guardrails(root)
    write(root, {"docs/guardrails/010.md": rule(10, ["I-107"], "Status: merged into 1")})
    assert cd.check_active_guardrails(root) != []
    # a hand edit of the generated file, or of its cut-off: drift
    cd.write_active_guardrails(root)
    text = active.read_text(encoding="utf-8")
    active.write_text(text + "11. **Always** a rule with no file\n", encoding="utf-8", newline="\n")
    assert cd.check_active_guardrails(root) != []
    cut_9 = text.replace("Reviewed through rule 10 ", "Reviewed through rule 9 ")
    active.write_text(cut_9, encoding="utf-8", newline="\n")
    assert cd.check_active_guardrails(root) != []
    no_cut = text.replace("Reviewed through rule 10 on", "Reviewed up to")
    active.write_text(no_cut, encoding="utf-8", newline="\n")
    assert any("no 'Reviewed through rule" in e for e in cd.check_active_guardrails(root))
    # the docs check runs it: ``main`` calls it, so it is a guard a rule can name
    assert cd.guard_exists("check", "check_active_guardrails")


def test_a_rule_added_after_the_review_needs_no_active_list_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """The owner's decision (#141): a pull request that adds a rule never touches a shared
    file (W-38), so main is never red between merges. A rule after the cut-off is a plain
    active rule, or carries a status line checked as usual, and ACTIVE.md stays as the
    review wrote it."""
    root = _reviewed(tmp_path, monkeypatch, cd)
    active = root / "docs" / "guardrails" / "ACTIVE.md"
    before = active.read_text(encoding="utf-8")
    write(
        root,
        {
            "docs/guardrails/101.md": rule(101, ["I-107"]),
            "docs/guardrails/102.md": rule(102, ["I-104"], "Status: merged into 1"),
            "docs/guardrails/103.md": rule(
                103, ["I-106"], "Status: retired: `test:test_the_guard_refuses` enforces it"
            ),
        },
    )
    assert cd.check_active_guardrails(root) == []
    assert cd.check_guardrails(root) == []
    assert active.read_text(encoding="utf-8") == before
    # its status line is checked as any rule's
    write(root, {"docs/guardrails/104.md": rule(104, ["I-107"], "Status: merged into 3")})
    errors = [e.removeprefix("docs/guardrails: ") for e in cd.check_guardrails(root)]
    assert errors == [
        "guardrail 104: merged into 3, which is not active; merge into an active rule"
    ]
    assert cd.check_active_guardrails(root) == []


def test_the_active_listing_includes_rules_after_the_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cd: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--active`` prints the one list a session reads: the active rules the review read
    and every rule added since with no status line, in number order, each as its file
    writes it."""
    root = _reviewed(tmp_path, monkeypatch, cd)
    write(
        root,
        {
            "docs/guardrails/101.md": rule(101, ["I-107"]),
            "docs/guardrails/102.md": rule(102, ["I-104"], "Status: merged into 1"),
            "docs/guardrails/110.md": rule(110, ["I-105"], verb="Never") + "\n",
        },
    )
    assert cd.main(["--active"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Active guardrails: 4 rules, reviewed through rule 10 on 2026-11-15, 2 ")
    listed = [ln.split(".", 1)[0] for ln in out.splitlines()[1:] if ln.strip()]
    assert listed == ["1", "10", "101", "110"]  # merged 2 and 102 and retired 3 left out
    assert rule(101, ["I-107"]).rstrip("\n") in out.splitlines()  # the file's own line
    assert cd.active_listing(root) == out


def test_a_ledger_entry_cited_only_by_a_retired_rule_is_still_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Invariant 4: retiring a rule never leaves its resolved ledger entries owed a rule.
    The retired file keeps its line, its ids and its guards."""
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105"]),
            "docs/guardrails/002.md": rule(
                2, ["I-106", "I-107"], "Status: retired: `test:test_the_guard_refuses` enforces it"
            ),
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    assert cd.check_guardrails(root) == []
    # and the retired rule's own guards are still checked
    write(
        root,
        {
            "docs/guardrails/002.md": (
                "2. **Never** keep rule 2 (I-106, I-107). Guard: `test:test_that_never_was`\n"
                "Status: retired: `test:test_the_guard_refuses` enforces it\n"
            )
        },
    )
    errors = cd.check_guardrails(root)
    assert errors == ["docs/guardrails: guardrail 2: test:test_that_never_was does not exist"]


@pytest.mark.parametrize(
    "status",
    [
        "Status: retired",
        "Status: retired: enforced by the hook",
        "Status: retired: `hook:push_gate`",
        "Status: merged into rule 1",
        "Status: merged into 1, mostly",
        "Status: active",
        "Status: retired: `manual:` a reason enforces it",
    ],
)
def test_a_malformed_status_line_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType, status: str
) -> None:
    """A second line that reads as a status but is neither form fails, so a typo never
    retires or merges a rule silently, or keeps it off the reading list."""
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105", "I-106", "I-107"]),
            "docs/guardrails/002.md": rule(2, ["I-104"], status),
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    errors = cd.check_guardrails(root)
    assert any(
        e.startswith("docs/guardrails: guardrail 2: the status line is malformed") for e in errors
    ), errors


def test_a_rule_file_holds_its_rule_line_and_at_most_a_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cd: ModuleType
) -> None:
    """Line one is the rule, line two its status; any other line fails, as before."""
    root = _root(
        tmp_path,
        {
            "docs/guardrails/001.md": rule(1, ["I-104", "I-105", "I-106", "I-107"]),
            "docs/guardrails/002.md": rule(2, ["I-104"]) + "a second thought\n",
            "docs/guardrails/003.md": rule(3, ["I-104"], "Status: merged into 1") + "more\n",
        },
    )
    monkeypatch.setattr(cd, "ROOT", root)
    errors = cd.check_guardrails(root)
    assert any(e.startswith("docs/guardrails/002.md: holds 2 lines") for e in errors), errors
    assert any(e.startswith("docs/guardrails/003.md: holds 3 lines") for e in errors), errors
