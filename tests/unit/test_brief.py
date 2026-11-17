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

import pytest

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


EDGE = "Edge decisions (decided before sending)"
SLOTS = ("Identity", "Failure", "Resume", "Pins")


def _fake_run(argv: list[str]) -> str:
    if argv[0] == "gh":
        return json.dumps(ISSUE)
    raise subprocess.CalledProcessError(128, argv)  # git: the fixture is no checkout


def _no_command(argv: list[str]) -> str:
    raise AssertionError(f"--check ran a command: {argv}")


def _brief(brief: ModuleType, tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    root = _repo(tmp_path / "repo")
    for rel, text in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    out = tmp_path / "brief.md"
    assert brief.main(["130", "--out", str(out)], run=_fake_run, root=root) == 0
    return out


def _slot(section: str, name: str) -> str:
    """One edge decision: from its label to the next one."""
    rest = section.split(f"- **{name}**", 1)[1]
    return rest.split("\n- **", 1)[0]


def test_the_brief_has_the_four_edge_decisions(tmp_path: Path) -> None:
    """The four cases agents needed a second round for are decided in the brief: the template
    and the filled brief both carry the section with its four slots. Identity, Failure and
    Resume are the orchestrator's (a ``TODO``); ``brief.py`` fills Pins. The report carries
    what the agent did for each."""
    brief = _load()
    template = (REPO_ROOT / ".claude" / "templates" / "agent-brief.md").read_text(encoding="utf-8")
    text = _brief(brief, tmp_path).read_text(encoding="utf-8")
    for source in (template, text):
        section = _section(source, EDGE)
        assert [n for n in SLOTS if f"- **{n}**" in section] == list(SLOTS)
        for name in SLOTS[:3]:
            assert _slot(section, name).count("TODO") == 1, name
    assert "TODO" not in _slot(_section(text, EDGE), "Pins")
    assert "`edge_decisions`" in _section(text, "Report")
    report = (REPO_ROOT / ".claude" / "templates" / "agent-report.md").read_text(encoding="utf-8")
    field = report.split("## edge_decisions", 1)[1].split("\n## ", 1)[0]
    assert all(name in field for name in SLOTS)


def test_a_brief_with_a_todo_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--check`` exits 1 and names each ``TODO`` by line and section, without ``gh``. The
    note ``brief.py`` writes at the top says "TODO" and is not one."""
    brief = _load()
    out = _brief(brief, tmp_path)
    capsys.readouterr()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert brief.main(["--check", str(out)], run=_no_command) == 1
    printed = capsys.readouterr().out
    for name in SLOTS[:3]:
        number = next(i for i, ln in enumerate(lines, 1) if ln.startswith(f"- **{name}**"))
        assert f"{out}:{number}: [{EDGE}]" in printed
    assert "[Decided design (not to be reopened)]" in printed
    assert "[Checks]" in printed and "[Invariants]" in printed
    assert f"{out}:1:" not in printed
    # one TODO left is enough
    one = tmp_path / "one.md"
    left = out.read_text(encoding="utf-8").replace("TODO", "done")
    one.write_text(
        left.replace("- **Resume**", "- **Resume** TODO", 1), encoding="utf-8", newline="\n"
    )
    assert brief.main(["--check", str(one)], run=_no_command) == 1
    printed = capsys.readouterr().out
    assert printed.count(f"{one}:") == 1 and f"[{EDGE}]" in printed


def test_a_filled_brief_passes_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = _load()
    out = _brief(brief, tmp_path)
    filled = tmp_path / "filled.md"
    filled.write_text(
        out.read_text(encoding="utf-8").replace("TODO", "decided"), encoding="utf-8", newline="\n"
    )
    assert brief.main(["--check", str(filled)], run=_no_command) == 0
    assert f"{filled}:" not in capsys.readouterr().out


def test_brief_lists_the_tests_outside_the_owned_files_that_pin_it(tmp_path: Path) -> None:
    """Pins: every test file outside the owned ones that imports an owned module or names an
    owned symbol, each with its reason, and each in the checks command. ``LoopLimits`` is a
    public name of the owned ``scio/loop/exits.py`` that the issue does not name."""
    brief = _load()
    extra = {
        "tests/integration/test_cli_text.py": 'EXPECTED = "LoopLimits(max_stall=2)"\n',
        **{
            f"tests/unit/test_many_{i:02}.py": "from scio.loop.exits import check_exit\n"
            for i in range(brief.MAX_TESTS + 1)
        },
    }
    text = _brief(brief, tmp_path, extra).read_text(encoding="utf-8")
    owned = _section(text, "Owned files")
    pins = _slot(_section(text, EDGE), "Pins")
    checks = _section(text, "Checks")
    assert "- `tests/integration/test_cli_text.py` (names `LoopLimits`)" in pins
    assert "test_cli_text.py" not in owned and "tests/integration/test_cli_text.py" in checks
    # past the cap on proposed owned tests, an importing test is a pin, never dropped
    assert "- `tests/unit/test_many_10.py` (imports `scio.loop.exits`" in pins
    assert "test_many_10.py" not in owned and "tests/unit/test_many_10.py" in checks
    # an owned test is not a pin; an unrelated test is neither
    assert "- `tests/unit/test_exits.py`" in owned and "test_exits.py" not in pins
    assert "test_other.py" not in pins and "test_other.py" not in checks
