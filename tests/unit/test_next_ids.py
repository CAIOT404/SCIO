"""`scripts/next_ids.py` (issue #122, W-21): the next ids come from every ref, never memory.

A handoff said "next I-205" while I-205 was in use on another branch. The next id is the
maximum at any local branch, any ``origin/*`` branch or any worktree's working files, plus
one; each step below adds an id in one more of those places and the answer follows it.
"""

from __future__ import annotations

from pathlib import Path

from tests.session_tools_support import (
    LEDGER,
    LESSONS,
    branch_commit,
    clone,
    commit,
    git,
    ledger,
    lessons,
    load,
    make_hub,
    write,
)


def test_next_ids_reads_every_branch_and_worktree(tmp_path: Path) -> None:
    ni = load("next_ids")
    hub = make_hub(tmp_path, ledger_last=5, guard_last=3)
    scio = clone(hub, tmp_path / "SCIO")
    # Mentions of I-099 in bodies and the retrospective's "40." are not ids.
    assert ni.next_ids(scio) == ("I-006", 4)

    # A branch that exists only on origin.
    branch_commit(hub, "feat/a", {LEDGER: ledger(6)})
    git(scio, "fetch", "-q", "origin")
    assert ni.next_ids(scio) == ("I-007", 4)

    # A local branch never pushed, with a new guardrail.
    git(scio, "checkout", "-q", "-b", "feat/b")
    commit(scio, {LEDGER: ledger(7), LESSONS: lessons(5)}, "I-007, guardrail 5")
    git(scio, "checkout", "-q", "main")
    assert ni.next_ids(scio) == ("I-008", 6)

    # A worktree's working files, not committed anywhere.
    wt = tmp_path / "wt-c"
    git(scio, "worktree", "add", "-q", "-b", "feat/c", str(wt))
    write(wt, {LEDGER: ledger(9)})
    assert ni.next_ids(scio) == ("I-010", 6)
    assert ni.next_ids(wt) == ("I-010", 6), "any worktree of the repository gives the same answer"

    # The fragments to come (W-38, W-39): a ledger fragment on an origin branch...
    branch_commit(hub, "feat/d", {"docs/ledger/I-011.md": "# I-011 · a fragment\n"})
    git(scio, "fetch", "-q", "origin")
    assert ni.next_ids(scio) == ("I-012", 6)
    # ...and a guardrail fragment in a worktree, named by its number or holding its rule.
    write(wt, {"docs/guardrails/007-never-guess.md": "7. **Never** guess an id.\n"})
    assert ni.next_ids(scio) == ("I-012", 8)
    write(wt, {"docs/guardrails/rule.md": "9. **Always** read every ref.\n"})
    assert ni.next_ids(scio) == ("I-012", 10)


def test_the_guardrail_count_stays_inside_its_section() -> None:
    ni = load("next_ids")
    assert ni.guardrail_max(lessons(4)) == 4
    assert ni.guardrail_max("# Lessons\n\n## M0\n\n12. **Always** a list item\n") == 0
    assert ni.ledger_max(ledger(3) + "See I-200 and ### not a heading I-300\n") == 3
