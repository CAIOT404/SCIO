"""`scripts/workflow_status.py` (issue #122): the Workflow v2 checklist's status is computed
from each item's evidence in the tree, never ticked, and a malformed checklist fails."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "workflow_status", REPO_ROOT / "scripts" / "workflow_status.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # a dataclass resolves its module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, checklist: str) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "WORKFLOW_V2.md").write_text(checklist, encoding="utf-8", newline="\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_present() -> None:\n    pass\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text(
        "def main() -> int:\n    return 0\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "CLAUDE.md").write_text("Rule: write it first.\n", encoding="utf-8", newline="\n")
    return tmp_path


ITEMS = """# Checklist

### W-01 · A rule that is written
- From: here
- Change: a rule
- Evidence: `grep:CLAUDE.md:write it first`

### W-02 · A tool with its test
- From: here
- Change: a tool
- Evidence: `symbol:scripts/tool.py:main`, `test:test_present`

### W-03 · A tool not built yet
- From: here
- Change: a tool
- Evidence: `symbol:scripts/later.py:main`, `test:test_present`

### W-04 · A GitHub setting
- From: here
- Change: a setting
- Evidence: `manual:a GitHub setting`

### W-05 · A setting already switched on
- From: here
- Change: a setting
- Evidence: `manual:a GitHub setting`
- Done: 2026-11-14 · the ruleset page

### W-06 · Covered elsewhere
- From: here
- Change: nothing
- Superseded by: W-02

## Next section
"""


def test_the_checklist_status_is_computed_from_evidence_not_ticked(tmp_path: Path) -> None:
    """Each item is done exactly when all its evidence holds. A manual item closes only with
    its Done line. A superseded item follows the item that covers it."""
    ws = _load()
    root = _tree(tmp_path, ITEMS)
    rows, errors = ws.report(root)
    assert errors == []
    assert {it.id: s for it, s in rows} == {
        "W-01": "done",
        "W-02": "done",
        "W-03": "open",  # one of two tokens holds: not done
        "W-04": "open",
        "W-05": "done",
        "W-06": "superseded",
    }
    # the evidence decides, in both directions: build the missing tool and W-03 closes
    (root / "scripts" / "later.py").write_text("def main():\n    pass\n", encoding="utf-8")
    assert dict((it.id, s) for it, s in ws.report(root)[0])["W-03"] == "done"


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("### W-07 · Gap\n- From: x\n- Change: x\n- Evidence: `file:x`\n", "without gaps"),
        ("### W-07 · No closer\n- From: x\n- Change: x\n", "exactly one of"),
        ("### W-07 · Odd kind\n- From: x\n- Change: x\n- Evidence: `url:x`\n", "unknown evidence"),
        (
            "### W-07 · Ticked\n- From: x\n- Change: x\n- Evidence: `file:x`\n- Done: today\n",
            "only for a manual",
        ),
        ("### W-07 · Nowhere\n- From: x\n- Change: x\n- Superseded by: W-99\n", "not an item"),
        (
            "### W-07 · Bad grep\n- From: x\n- Change: x\n- Evidence: `grep:CLAUDE.md`\n",
            "needs <path>",
        ),
    ],
)
def test_a_malformed_checklist_is_reported(tmp_path: Path, broken: str, expected: str) -> None:
    ws = _load()
    text = ITEMS.replace("## Next section", broken + "\n## Next section")
    if "Gap" in broken:
        text = text.replace("### W-07", "### W-08")
    errors = ws.problems(ws.parse(text))
    assert any(expected in e for e in errors), errors


def test_the_session_hook_prints_the_count_and_the_next_open_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _load()
    root = _tree(tmp_path, ITEMS)
    real_report = ws.report
    monkeypatch.setattr(ws, "report", lambda: real_report(root))
    assert ws.main(["--session"]) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "4/6 closed" in context and "W-03" in context and "W-04" in context
    assert "W-01" not in context  # done items are not repeated
    assert "never tick them by hand" in context


def test_the_real_checklist_is_well_formed_and_accounts_for_every_item() -> None:
    """The repository's own checklist parses, has no problems, and starts at W-01."""
    ws = _load()
    rows, errors = ws.report(REPO_ROOT)
    assert errors == []
    assert rows and rows[0][0].id == "W-01"
    assert all(s in ("done", "open", "superseded", "dropped") for _, s in rows)
