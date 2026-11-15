"""``scripts/brief.py`` (issue #122, W-33): the agent brief filled from an issue.

The issue comes from a faked ``gh``; the tree is a small fixture repository, so what the
brief proposes does not move when SCIO's own docs change.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from scio.settings import REPO_ROOT

ISSUE = {
    "number": 130,
    "title": "Halt a stalled feature only after the ladder",
    "labels": [{"name": "enhancement"}],
    "body": "\n".join(
        [
            "### Goal",
            "A stall halts a feature only after the ladder fails.",
            "",
            "### Evidence",
            "- `scio/loop/exits.py` decides the stall: `check_exit` counts it.",
            "- The owner, 2026-11-14: “halt only after that fails”.",
            '- The coder\'s summary said "stopped before writing any production code".',
            "- `scio/loop/ladder.py` is new.",
            "- The product's `graphcalc/calc.py` was edited by the debugger.",
        ]
    ),
}

FILES = {
    "scio/loop/exits.py": "class LoopLimits:\n    pass\n\n\ndef check_exit(p, b, l):\n"
    "    return None\n",
    "scio/other.py": "def unrelated():\n    return 1\n",
    "docs/ARCHITECTURE.md": "# Architecture\n\nThe loop.\n\n- `scio/loop/exits.py`: the exit "
    "conditions.\n",
    "docs/adr/0006-stall.md": "# 0006 Stall\n\n`check_exit` counts a stall.\n",
    "configs/budgets.yaml": "max_stall: 2\n# max_stall feeds check_exit\ncheck_exit: 1\n",
    "README.md": "# SCIO\n\nNothing about it here.\n",
    "tests/unit/test_exits.py": "from scio.loop.exits import check_exit\n",
    "tests/unit/test_other.py": "from scio.other import unrelated\n",
}


def _load() -> ModuleType:
    name = "scio_scripts_brief"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "brief.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # its dataclass resolves its string annotations through it
    spec.loader.exec_module(mod)
    return mod


def _repo(root: Path) -> Path:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def test_brief_lists_owned_files_docs_touchpoints_and_invariant_tests(tmp_path: Path) -> None:
    """W-33: an issue naming ``scio/loop/exits.py`` and quoting the owner gives a brief that
    owns that file and the test that imports it, lists the docs lines that describe it, and
    has exactly one invariant slot, for the owner's sentence: the coder's quoted summary is
    evidence, not an invariant."""
    brief = _load()
    root = _repo(tmp_path / "repo")
    seen: list[list[str]] = []

    def fake_run(argv: list[str]) -> str:
        seen.append(argv)
        if argv[0] == "gh":
            return json.dumps(ISSUE)
        raise subprocess.CalledProcessError(128, argv)  # git: the fixture is no checkout

    out = tmp_path / "brief.md"
    assert brief.main(["130", "--out", str(out)], run=fake_run, root=root) == 0
    assert seen[0] == ["gh", "issue", "view", "130", "--json", "number,title,body,labels"]
    text = out.read_text(encoding="utf-8")
    assert "{{" not in text and text.startswith("<!--")
    assert "# Brief: #130 Halt a stalled feature only after the ladder" in text
    assert "`feat/130-halt-a-stalled-feature`" in text

    owned = _section(text, "Owned files")
    assert "- `scio/loop/exits.py` (named in the issue)" in owned
    assert "- `scio/loop/ladder.py` (new: named in the issue)" in owned
    assert "scio/other.py" not in owned and "graphcalc" not in owned  # another repository's
    # the docs touchpoints: the architecture line, the ADR, a config comment (not its value)
    assert "- `docs/ARCHITECTURE.md:5` (`scio/loop/exits.py`)" in owned
    assert "- `docs/adr/0006-stall.md:3` (`check_exit`)" in owned
    assert "`configs/budgets.yaml:2`" in owned and "`configs/budgets.yaml:3`" not in owned
    assert "README.md" not in owned
    # the tests: the one that imports the owned module
    assert "- `tests/unit/test_exits.py`" in owned and "test_other.py" not in owned

    invariants = _section(text, "Invariants")
    assert invariants.count("- test: `TODO test_") == 1
    assert "“halt only after that fails”" in invariants
    assert "stopped before writing" not in invariants
    evidence = _section(text, "Evidence (verbatim, with paths)")
    assert "stopped before writing" in evidence
    assert "- `graphcalc/calc.py` (not in this repository)" in evidence
    assert "TODO" in _section(text, "Decided design (not to be reopened)")
    assert "tests/unit/test_exits.py" in _section(text, "Checks")


def test_an_owner_blockquote_is_an_invariant_and_a_bare_file_name_is_resolved(
    tmp_path: Path,
) -> None:
    """A blockquote after a line naming the owner is the owner's words. A bare file name that
    is unique in the tree resolves to its path."""
    brief = _load()
    body = "The owner, 2026-11-14:\n> lets make every change you just suggested\n\nSee exits.py."
    assert brief.owner_quotes(body) == ["lets make every change you just suggested"]
    assert brief.owner_quotes('The log said "the session hit its cap".') == []
    root = _repo(tmp_path / "repo")
    prop = brief.propose(body, root, sorted(FILES))
    assert prop.paths == {"scio/loop/exits.py": True}
