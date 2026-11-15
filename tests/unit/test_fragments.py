"""Ledger entries, guardrails and changelog entries are fragments (issue #122, W-38 to W-43).

Every pull request used to append to the same three files (``docs/ISSUE_LEDGER.md``, the
guardrail list in ``docs/LESSONS_LEARNED.md`` and CHANGELOG.md's ``[Unreleased]``), so every
merge conflicted, and one resolution dropped a status line (I-206). Now each entry, rule and
change is its own file: ``docs/ledger/I-NNN.md``, ``docs/guardrails/NNN.md`` and
``changes/unreleased/<n>-<slug>.<kind>.md``. These tests hold every reader to the fragments,
the release cut to assembling them, and the migration to moving the old text byte for byte.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.session_tools_support import branch_commit, commit, git, load, make_hub, write

BASE_DOCS = {
    "docs/ISSUE_LEDGER.md": "# Issue ledger\n\nThe entries are files in docs/ledger/.\n",
    "docs/LESSONS_LEARNED.md": (
        "# Lessons\n\n## Guardrails (Always / Never)\n\nHow the rules work.\n\n"
        "## M0: design\n\n1. a retrospective's list item, not a rule\n"
    ),
    "docs/ledger/README.md": "# Entries\n\n```\n### I-NNN · <short title>\n```\n",
    "docs/ledger/I-104.md": (
        "### I-104 · identity\n- Fix: x\n- Resolved: 2026-10-27 · Status: resolved\n"
    ),
    "docs/guardrails/001.md": (
        "1. **Never** guess the identity (I-104). Guard: `manual:` a property of the machine\n"
    ),
    "CHANGELOG.md": (
        "# Changelog\n\n## [Unreleased]\n\nThe changes are files in `changes/unreleased/`.\n\n"
        "## [0.1.0] - 2026-10-10\n### Added\n- First release.\n"
    ),
}


def _docs_root(tmp_path: Path) -> Path:
    """A repository holding only the docs the checks read, in the fragment layout."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, BASE_DOCS)
    return root


def test_every_ledger_reader_reads_the_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-38: the guardrails check, the status check and ``next_ids`` all read
    ``docs/ledger/``. An entry that exists only as a fragment is seen by each of them."""
    root = _docs_root(tmp_path)
    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    assert cd.check_guardrails() == []
    assert cd.check_ledger_statuses() == []
    write(
        root,
        {
            "docs/ledger/I-300.md": (
                "### I-300 · only a fragment\n- Fix: y\n- Resolved: 2026-11-15 · Status: resolved\n"
            ),
            "docs/ledger/I-301.md": "### I-301 · no status line yet\n- Fix: not yet\n",
        },
    )
    guardrails = cd.check_guardrails()
    assert any("I-300 is resolved but no guardrail cites it" in e for e in guardrails), guardrails
    statuses = cd.check_ledger_statuses()
    assert any("I-301 has no '- Resolved:" in e for e in statuses), statuses
    next_ids = load("next_ids")
    assert next_ids.next_ids(root)[0] == "I-302"


def test_guardrails_are_read_from_their_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-39: ``check_guardrails`` reads ``docs/guardrails/NNN.md``, one rule per file, in
    number order. A fragment's guard must exist, and a misnamed fragment is reported."""
    root = _docs_root(tmp_path)
    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    write(
        root,
        {
            "docs/guardrails/010.md": (
                "10. **Always** check the guard (I-105). Guard: `check:check_that_never_was`\n"
            ),
            "docs/guardrails/002.md": "2. **Never** skip a rule (I-104). Guard: `manual:` why\n",
            "docs/ledger/I-105.md": (
                "### I-105 · cited\n- Resolved: 2026-10-27 · Status: resolved\n"
            ),
            "docs/ledger/I-106.md": (
                "### I-106 · uncited\n- Resolved: 2026-10-27 · Status: resolved\n"
            ),
        },
    )
    rules = cd.guardrail_fragment_rules(root)
    assert [r.split(".", 1)[0] for r in rules] == ["1", "2", "10"]  # by number, not by text
    errors = cd.check_guardrails()
    assert any("guardrail 10: check:check_that_never_was does not exist" in e for e in errors)
    assert any("I-106 is resolved but no guardrail cites it" in e for e in errors)
    assert not any("I-105 is resolved" in e for e in errors), errors
    write(
        root,
        {
            "docs/guardrails/3.md": "3. **Always** pad (I-104). Guard: `manual:` why\n",
            "docs/guardrails/004.md": "5. **Never** renumber (I-104). Guard: `manual:` why\n",
        },
    )
    errors = cd.check_guardrails()
    assert any(e.startswith("docs/guardrails/3.md: ") for e in errors), errors
    assert any(e.startswith("docs/guardrails/004.md: ") and "5" in e for e in errors), errors


def test_release_cut_assembles_the_changelog_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-40: ``release_notes.py --cut`` writes the new section from the fragments, Added,
    Changed and Fixed, each in its number order, deletes them, and the released section then
    stays what its tag shipped (``check_released_section_frozen``, I-089)."""
    root = _docs_root(tmp_path)
    folder = root / "changes" / "unreleased"
    write(
        root,
        {
            "changes/unreleased/010-ten.added.md": "Tenth, which sorts after the second.\n",
            "changes/unreleased/002-two.added.md": "Second.\n",
            "changes/unreleased/001-nested.changed.md": (
                "A change with a list:\n- one\n- two\n\nAnd a paragraph of the same change.\n"
            ),
            "changes/unreleased/003-a-fix.fixed.md": "A fix (I-300).\n",
        },
    )
    commit(root, {}, "the release's changes")
    rn = load("release_notes")
    monkeypatch.setattr(rn, "CHANGELOG", root / "CHANGELOG.md")
    monkeypatch.setattr(rn, "CHANGES", folder, raising=False)
    assert rn.main(["--cut", "0.2.0"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[0\.2\.0\] - (\d{4}-\d{2}-\d{2})$", text, re.MULTILINE)
    assert m, text
    body = (
        "### Added\n- Second.\n- Tenth, which sorts after the second.\n\n"
        "### Changed\n- A change with a list:\n  - one\n  - two\n\n"
        "  And a paragraph of the same change.\n\n"
        "### Fixed\n- A fix (I-300).\n"
    )
    assert text == (
        "# Changelog\n\n## [Unreleased]\n\nThe changes are files in `changes/unreleased/`.\n\n"
        f"## [0.2.0] - {m.group(1)}\n{body}\n"
        "## [0.1.0] - 2026-10-10\n### Added\n- First release.\n"
    )
    assert capsys.readouterr().out == body
    assert sorted(p.name for p in folder.glob("*")) == [], "the cut deletes the fragments"

    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    commit(root, {}, "release 0.2.0")
    git(root, "tag", "v0.2.0")
    assert cd.check_released_section_frozen() == []
    assert cd.check_changelog() == []
    commit(root, {"changes/unreleased/001-later.fixed.md": "A later fix.\n"}, "a later PR")
    assert cd.check_released_section_frozen() == [], "a new fragment never touches a release"
    moved = text.replace("- A fix (I-300).\n", "- A fix (I-300).\n- A later fix.\n")
    write(root, {"CHANGELOG.md": moved})
    assert cd.check_released_section_frozen(), "the check still catches an edited release"


def test_ledger_new_writes_an_open_entry_with_its_status_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-41: ``ledger_new.py`` takes the next id from every branch and worktree, writes its
    fragment with every field ``TODO`` and ``- Resolved: - · Status: open``, and prints the
    id. A second call takes the next id; it never overwrites an entry."""
    hub = make_hub(tmp_path, ledger_last=5)  # I-001 to I-005 in the old ledger on main
    branch_commit(hub, "feat/other", {"docs/ledger/I-009.md": "### I-009 · elsewhere\n"})
    ln = load("ledger_new")
    argv = ["--root", str(hub), "--title", "A hook refused a push", "--where", "push_gate.py"]
    assert ln.main(argv) == 0
    assert capsys.readouterr().out.strip() == "I-010"
    text = (hub / "docs" / "ledger" / "I-010.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "### I-010 · A hook refused a push"
    assert re.fullmatch(r"- Found: \d{4}-\d{2}-\d{2} · TODO · push_gate\.py", lines[1]), lines[1]
    assert lines[2:7] == [
        "- Symptom: TODO",
        "- Root cause: TODO",
        "- Fix: TODO",
        "- Sweep: TODO",
        "- Guard: TODO",
    ]
    assert lines[7:] == ["- Resolved: - · Status: open", ""], "ends with its status line"
    cd = load("check_docs")
    assert cd.ledger_status_problems(text) == []
    assert cd.resolved_ledger_ids(text, 1) == []
    assert ln.main(argv) == 0
    assert capsys.readouterr().out.strip() == "I-011"
    assert (hub / "docs" / "ledger" / "I-010.md").read_text(encoding="utf-8") == text


def test_a_todo_left_in_a_resolved_entry_fails_the_docs_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-41: the scaffold's ``TODO`` fields are fine while the entry is open. An entry closed
    with one left is not finished, and the docs check says which."""
    cd = load("check_docs")
    scaffold = (
        "### I-300 · t\n- Found: 2026-11-15 · TODO · x\n- Symptom: seen\n- Fix: TODO\n"
        "- Resolved: - · Status: open\n"
    )
    assert cd.ledger_status_problems(scaffold) == []
    closed = scaffold.replace(
        "- Resolved: - · Status: open", "- Resolved: 2026-11-15 · Status: resolved"
    )
    problems = cd.ledger_status_problems(closed)
    assert len(problems) == 1 and problems[0].startswith("I-300 ") and "TODO" in problems[0]
    prose = (
        "### I-301 · a TODO comment\n- Symptom: a TODO comment in the code was read as a task\n"
        "- Fix: read only the marker\n- Resolved: 2026-11-15 · Status: resolved\n"
    )
    assert cd.ledger_status_problems(prose) == [], "a field that mentions TODO is not one"
    root = _docs_root(tmp_path)
    monkeypatch.setattr(cd, "ROOT", root)
    write(root, {"docs/ledger/I-300.md": closed})
    assert any("I-300" in e and "TODO" in e for e in cd.check_ledger_statuses())


def test_an_entry_rule_or_change_left_in_the_old_files_fails_the_docs_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch that predates the fragments may still append the old way. Its entry, rule or
    change would be invisible to every reader, so the docs check names it instead."""
    root = _docs_root(tmp_path)
    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    ledger = BASE_DOCS["docs/ISSUE_LEDGER.md"] + (
        "\n### I-400 · written the old way\n- Resolved: 2026-11-15 · Status: resolved\n"
    )
    lessons = BASE_DOCS["docs/LESSONS_LEARNED.md"].replace(
        "How the rules work.\n",
        "How the rules work.\n\n94. **Always** a rule the old way (I-400). Guard: `manual:` x\n",
    )
    changelog = BASE_DOCS["CHANGELOG.md"].replace(
        "`changes/unreleased/`.\n", "`changes/unreleased/`.\n\n### Fixed\n- An old-style bullet.\n"
    )
    write(
        root,
        {
            "docs/ISSUE_LEDGER.md": ledger,
            "docs/LESSONS_LEARNED.md": lessons,
            "CHANGELOG.md": changelog,
        },
    )
    statuses = cd.check_ledger_statuses()
    assert any(e.startswith("docs/ISSUE_LEDGER.md: ") and "I-400" in e for e in statuses)
    guardrails = cd.check_guardrails()
    assert any(e.startswith("docs/LESSONS_LEARNED.md: ") and "94" in e for e in guardrails)
    changes = cd.check_changelog()
    assert any(e.startswith("CHANGELOG.md: ") and "An old-style bullet" in e for e in changes)
    assert all("migrate_fragments.py" in e for e in [*statuses, *guardrails, *changes])


def test_a_change_fragment_links_from_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change fragment's text lands in CHANGELOG.md at the root, so its links are read from
    there. A link written for the fragment's own folder would break at the cut."""
    root = _docs_root(tmp_path)
    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    write(
        root,
        {
            "docs/RECOVERY.md": "# Recovery\n",
            "changes/unreleased/001-link.fixed.md": "See [the guide](docs/RECOVERY.md).\n",
        },
    )
    assert cd.check_links() == []
    write(
        root,
        {"changes/unreleased/002-up.fixed.md": "See [the guide](../../docs/RECOVERY.md).\n"},
    )
    assert cd.check_links() == [
        "changes/unreleased/002-up.fixed.md: broken link -> ../../docs/RECOVERY.md"
    ]


OLD_LEDGER = (
    "# Issue ledger\n\n"
    "Running log of every problem. Statuses: `open`, `resolved`, `wontfix` (with reason).\n\n"
    "Template:\n\n"
    "```\n### I-NNN · <short title>\n- Found: <date> · <milestone> · <where>\n"
    "- Resolved: <date> · Status: open | resolved | wontfix\n```\n\n"
    "---\n\n"
    "### I-002 · listed before I-001, as I-046 was before I-044\n"
    "- Found: 2026-10-10 · M1 · `C:/Users/x` and a `·` in the text\n"
    "- Resolved: 2026-10-10 · Status: resolved\n\n"
    "### I-001 · a line after its status line, as in I-019\n"
    "- Found: 2026-10-10 · M1 · y\n"
    "- Resolved: 2026-10-11 · Status: resolved\n"
    "- Re-opened and resolved 2026-10-12: the line I-019 has.\n"
    "### I-104 · no blank line above, as after I-058\n"
    "- Found: 2026-10-27 · M11 · z\n"
    "- Resolved: - · Status: open\n"
)
OLD_LESSONS = (
    "# Lessons learned\n\nRead it all. Ids are in [ISSUE_LEDGER.md](ISSUE_LEDGER.md).\n\n"
    "## Guardrails (Always / Never)\n\n"
    "Every bug becomes one rule here. How the rules work.\n\n"
    "1. **Never** a (I-001). Guard: `manual:` reason\n"
    "2. **Always** b (I-002). Guard: `manual:` reason\n"
    "10. **Always** c (I-104). Guard: `manual:` reason\n\n"
    "## Standing pre-flight list\n\n1. not a rule\n"
)
OLD_CHANGELOG = (
    "# Changelog\n\nIntro.\n\n"
    "## [Unreleased]\n"
    "### Changed\n"
    "- The gate is quick (W-11):\n  - It runs lint.\n  - A pass stamps its tree.\n\n"
    "### Fixed\n"
    "- A retry starts from the verified commit (I-216).\n\n"
    "### Added\n"
    "- Session tools:\n  - `session_state.py` (W-20).\n\n  A second paragraph of the same bullet.\n"
    "- `docs/WORKFLOW_V2.md`, the checklist.\n\n"
    "### Changed\n"
    "- CI runs in three shards.\n\n"
    "## [0.1.0] - 2026-10-10\n### Added\n- First release.\n"
)


def _old_root(tmp_path: Path) -> Path:
    root = tmp_path / "old"
    write(
        root,
        {
            "docs/ISSUE_LEDGER.md": OLD_LEDGER,
            "docs/LESSONS_LEARNED.md": OLD_LESSONS,
            "CHANGELOG.md": OLD_CHANGELOG,
        },
    )
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _old_entries(text: str) -> dict[str, str]:
    """The entries as the old ledger held them: a heading up to the next one, without the
    blank lines that separated them."""
    heads = list(re.finditer(r"^### (I-\d+) ", text, re.MULTILINE))
    return {
        m.group(1): text[
            m.start() : (heads[i + 1].start() if i + 1 < len(heads) else len(text))
        ].rstrip("\n")
        + "\n"
        for i, m in enumerate(heads)
    }


def test_the_migration_moves_every_entry_rule_and_change_byte_for_byte_and_reruns_as_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``scripts/migrate_fragments.py`` splits the three files into fragments whose text is
    the old text, leaves the files in their short form, and changes nothing on a rerun."""
    root = _old_root(tmp_path)
    mf = load("migrate_fragments")
    changed = mf.migrate(root)
    assert "docs/ledger/I-001.md" in changed and "CHANGELOG.md" in changed

    ledger_dir = root / "docs" / "ledger"
    assert sorted(p.name for p in ledger_dir.glob("I-*.md")) == ["I-001.md", "I-002.md", "I-104.md"]
    for lid, entry in _old_entries(OLD_LEDGER).items():
        assert (ledger_dir / f"{lid}.md").read_bytes() == entry.encode("utf-8"), lid
    short = (root / "docs" / "ISSUE_LEDGER.md").read_text(encoding="utf-8")
    assert short.startswith("# Issue ledger\n\nRunning log of every problem.")
    assert "docs/ledger/" in short and "### I-" not in short and "Template:" not in short
    assert "### I-NNN · <short title>" in (ledger_dir / "README.md").read_text(encoding="utf-8")

    rules = {
        p.name: p.read_text(encoding="utf-8") for p in (root / "docs" / "guardrails").glob("*")
    }
    old_rules = [ln + "\n" for ln in OLD_LESSONS.splitlines() if re.match(r"^\d+\. \*\*", ln)]
    assert rules == dict(zip(["001.md", "002.md", "010.md"], old_rules, strict=True))
    lessons = (root / "docs" / "LESSONS_LEARNED.md").read_text(encoding="utf-8")
    assert "Every bug becomes one rule here. How the rules work.\n\n" in lessons
    assert "docs/guardrails/" in lessons and "**Never** a (I-001)" not in lessons
    assert lessons.endswith("## Standing pre-flight list\n\n1. not a rule\n")

    rn = load("release_notes")
    fragments = rn.change_fragments(root / "changes" / "unreleased")
    kinds = [rn.CHANGE_FRAGMENT_RE.match(p.name).group(3) for p in fragments]
    assert kinds == ["changed", "fixed", "added", "added", "changed"], "document order"
    by_kind: dict[str, list[str]] = {}
    for kind, p in zip(kinds, fragments, strict=True):
        by_kind.setdefault(kind, []).append(rn.bullet(p.read_text(encoding="utf-8")))
    assert by_kind == {
        "changed": [
            "- The gate is quick (W-11):\n  - It runs lint.\n  - A pass stamps its tree.",
            "- CI runs in three shards.",
        ],
        "fixed": ["- A retry starts from the verified commit (I-216)."],
        "added": [
            "- Session tools:\n  - `session_state.py` (W-20).\n\n"
            "  A second paragraph of the same bullet.",
            "- `docs/WORKFLOW_V2.md`, the checklist.",
        ],
    }
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith("# Changelog\n\nIntro.\n\n## [Unreleased]\n\n")
    assert "changes/unreleased/" in changelog and "- The gate is quick" not in changelog
    assert changelog.endswith("\n\n## [0.1.0] - 2026-10-10\n### Added\n- First release.\n")

    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    assert sorted(cd.resolved_ledger_ids(cd.read_ledger(root), 1)) == ["I-001", "I-002"]
    assert cd.check_ledger_statuses() == []
    assert cd.check_changelog() == []

    before = _snapshot(root)
    assert mf.migrate(root) == []
    assert _snapshot(root) == before, "a rerun changes nothing"


def test_a_late_old_style_append_migrates_into_fragments_with_one_pointer_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch that predates the fragments appends the old way after the migration landed;
    the docs check names it and says to run the migration again. That run moves the entry,
    the rule and the change into fragments, a change after the ones already there, and
    leaves one pointer in each file, never a second."""
    root = _old_root(tmp_path)
    mf = load("migrate_fragments")
    mf.migrate(root)
    ledger = root / "docs" / "ISSUE_LEDGER.md"
    lessons = root / "docs" / "LESSONS_LEARNED.md"
    changelog = root / "CHANGELOG.md"
    late_entry = "### I-400 · appended late\n- Fix: x\n- Resolved: 2026-11-15 · Status: resolved\n"
    late_rule = "11. **Always** append late (I-400). Guard: `manual:` a merge order"
    late_lessons = lessons.read_text(encoding="utf-8").replace(
        "\n## Standing", f"{late_rule}\n\n## Standing"
    )
    late_changelog = changelog.read_text(encoding="utf-8").replace(
        "\n## [0.1.0]", "### Fixed\n- A late fix.\n\n## [0.1.0]"
    )
    write(
        root,
        {
            "docs/ISSUE_LEDGER.md": ledger.read_text(encoding="utf-8") + late_entry,
            "docs/LESSONS_LEARNED.md": late_lessons,
            "CHANGELOG.md": late_changelog,
        },
    )
    written = mf.migrate(root)
    assert "docs/ledger/I-400.md" in written and "docs/guardrails/011.md" in written
    assert (root / "docs" / "ledger" / "I-400.md").read_text(encoding="utf-8") == late_entry
    assert (root / "docs" / "guardrails" / "011.md").read_text(encoding="utf-8") == (
        late_rule + "\n"
    )
    late = [p.name for p in (root / "changes" / "unreleased").glob("*.fixed.md")]
    assert sorted(late) == [
        "002-a-retry-starts-from-the-verified.fixed.md",
        "006-a-late-fix.fixed.md",
    ]
    assert ledger.read_text(encoding="utf-8").count(mf.LEDGER_MARK) == 1
    assert lessons.read_text(encoding="utf-8").count(mf.GUARDRAIL_MARK) == 1
    assert "\n\n\n" not in lessons.read_text(encoding="utf-8")
    assert changelog.read_text(encoding="utf-8").count(mf.CHANGES_POINTER) == 1
    cd = load("check_docs")
    monkeypatch.setattr(cd, "ROOT", root)
    assert cd.check_ledger_statuses() == []
    assert cd.check_guardrails() == []
    assert cd.check_changelog() == []
    assert mf.migrate(root) == []


def test_the_migration_refuses_an_entry_that_differs_from_its_fragment(tmp_path: Path) -> None:
    """A rerun over a tree that already holds a fragment never overwrites it with a stale copy
    of the entry (the I-206 failure): it stops, names the id and writes nothing."""
    root = _old_root(tmp_path)
    mf = load("migrate_fragments")
    stale = "### I-001 · a line after its status line, as in I-019\n- Resolved: - · Status: open\n"
    write(root, {"docs/ledger/I-001.md": stale})
    before = _snapshot(root)
    with pytest.raises(mf.MigrationError, match="I-001"):
        mf.migrate(root)
    assert _snapshot(root) == before
    assert mf.main(["--root", str(root)]) == 1


def test_a_repair_pr_must_add_a_ledger_fragment() -> None:
    """W-43: the repair session writes its entry as ``docs/ledger/I-NNN.md``, and the
    supervisor's check of its claim looks for that file, not for the old ledger."""
    from recovery.brief import READ_FIRST
    from recovery.repair import adds_ledger_entry

    assert adds_ledger_entry(["scio/x.py", "tests/unit/test_x.py", "docs/ledger/I-219.md"])
    assert not adds_ledger_entry(["scio/x.py", "docs/ISSUE_LEDGER.md"])
    assert not adds_ledger_entry(["docs/ledger/README.md"])
    assert "docs/ledger/README.md" in [path for path, _why in READ_FIRST]
