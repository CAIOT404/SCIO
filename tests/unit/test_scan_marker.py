"""I-234: an agent runs only its own test files, so a test that scans the whole codebase for a
forbidden construct failed only in the orchestrator's gate (guardrail 74's
``test_no_subprocess_timeout_outside_run_tree`` caught the #141 dashboard agent's
``subprocess.run(..., timeout=30)``). Every such test carries ``@pytest.mark.scan``, and the
agent-brief template's checks run ``pytest -m scan``.

The detector is deliberately simple. A test function is a codebase scan when it calls
``.rglob(...)`` on, or ``os.walk(...)`` over, a path built from the repository root, either in
its own body or through a helper defined in the same module, or when it reads a module-level
name whose value is such a call. A name is root-derived when its assignment mentions
``REPO_ROOT`` (imported from ``scio.settings``), ``Path(__file__)...parents``, or another
root-derived name. A fixture such as ``tmp_path`` is never assigned, so a test that globs its
own temporary tree is not a scan. A plain ``.glob`` is not followed: mark such a scan by hand
(``test_budget_guards.py`` does).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".claude" / "templates" / "agent-brief.md"
SCAN_LINE = "uv run python -m pytest -q -n 4 -m scan"
IMPORTED_ROOTS = frozenset({"REPO_ROOT"})
MARK = "pytest.mark.scan"
FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


def _is_rooted(expr: ast.AST, names: set[str]) -> bool:
    """``expr`` builds a path from the repository root."""
    seen = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
    from_file = "__file__" in seen and any(
        isinstance(n, ast.Attribute) and n.attr == "parents" for n in ast.walk(expr)
    )
    return from_file or bool(seen & names)


def _rooted_names(body: list[ast.stmt], known: set[str]) -> set[str]:
    """``known`` plus every name ``body`` assigns from a root-derived value (to a fixed point,
    so a chain such as ``root = ROOT / "scio"; target = root / "x"`` is followed)."""
    names = set(known)
    assigns = [
        (n.targets if isinstance(n, ast.Assign) else [n.target], n.value)
        for stmt in body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None
    ]
    while True:
        new = {
            t.id
            for targets, value in assigns
            if _is_rooted(value, names)
            for t in targets
            if isinstance(t, ast.Name) and t.id not in names
        }
        if not new:
            return names
        names |= new


def _scans(node: ast.AST, names: set[str]) -> bool:
    """``node`` holds an ``rglob`` on, or an ``os.walk`` over, a root-derived path."""
    for call in ast.walk(node):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        func = call.func
        if func.attr == "rglob" and _is_rooted(func.value, names):
            return True
        is_walk = func.attr == "walk" and isinstance(func.value, ast.Name) and func.value.id == "os"
        if is_walk and call.args and _is_rooted(call.args[0], names):
            return True
    return False


def codebase_scans(source: str) -> dict[str, bool]:
    """Each test function in ``source`` that scans the codebase, with whether it is marked
    ``@pytest.mark.scan``."""
    tree = ast.parse(source)
    module_names = _rooted_names(
        [s for s in tree.body if not isinstance(s, FuncDef | ast.ClassDef)], set(IMPORTED_ROOTS)
    )
    # module-level names whose value is itself a scan (``FILES = sorted(ROOT.rglob(...))``)
    scan_names = {
        t.id
        for s in tree.body
        if isinstance(s, ast.Assign) and _scans(s.value, module_names)
        for t in s.targets
        if isinstance(t, ast.Name)
    }
    funcs = [n for n in ast.walk(tree) if isinstance(n, FuncDef)]
    scanning = {
        f.name
        for f in funcs
        if _scans(f, _rooted_names(f.body, module_names))
        or any(isinstance(n, ast.Name) and n.id in scan_names for n in ast.walk(f))
    }
    # one function calling another of the same module that scans (a ``_code_files()`` helper)
    while True:
        callers = {
            f.name
            for f in funcs
            if f.name not in scanning
            and any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in scanning
                for c in ast.walk(f)
            )
        }
        if not callers:
            break
        scanning |= callers
    return {
        f.name: any(ast.unparse(d) == MARK for d in f.decorator_list)
        for f in funcs
        if f.name.startswith("test_") and f.name in scanning
    }


def _test_modules() -> list[Path]:
    return sorted(p for p in (ROOT / "tests").rglob("test_*.py") if "__pycache__" not in p.parts)


def _checks_block(template: str) -> str:
    section = template.split("## Checks", 1)[1].split("\n## ", 1)[0]
    fenced = re.search(r"```\n(.*?)```", section, re.DOTALL)
    assert fenced, "the agent brief's checks section has no command block"
    return fenced.group(1)


def test_the_agent_brief_runs_the_codebase_scans() -> None:
    block = _checks_block(TEMPLATE.read_text(encoding="utf-8"))
    assert any(line.strip().startswith(SCAN_LINE) for line in block.splitlines()), block


@pytest.mark.scan
def test_every_codebase_scan_carries_the_scan_marker() -> None:
    found = {
        f"{path.relative_to(ROOT).as_posix()}::{name}": marked
        for path in _test_modules()
        for name, marked in codebase_scans(path.read_text(encoding="utf-8")).items()
    }
    # the detector is not blind: guardrail 74's scan, the one that caught I-234, is in it
    assert "tests/unit/test_proctree.py::test_no_subprocess_timeout_outside_run_tree" in found
    unmarked = sorted(test for test, marked in found.items() if not marked)
    assert unmarked == [], f"add @pytest.mark.scan to each (I-234): {unmarked}"


def test_the_detector_sees_a_scan_through_a_helper_and_skips_a_tmp_tree() -> None:
    source = "\n".join(
        [
            "import os, pytest",
            "from pathlib import Path",
            "from scio.settings import REPO_ROOT",
            "ROOT = Path(__file__).resolve().parents[2]",
            "PACKAGE = ROOT / 'recovery'",
            "FILES = sorted((REPO_ROOT / 'scio').glob('*.py'))",
            "def _code():",
            "    root = ROOT / 'scio'",
            "    return list(root.rglob('*.py'))",
            "def test_direct():",
            "    assert list(PACKAGE.rglob('*.py'))",
            "@pytest.mark.scan",
            "def test_through_a_helper():",
            "    assert _code()",
            "def test_walk():",
            "    assert list(os.walk(REPO_ROOT / 'scripts'))",
            "def test_tmp(tmp_path):",
            "    repo = tmp_path / 'repo'",
            "    assert not list(repo.rglob('*'))",
            "def test_plain_glob():",
            "    assert FILES",
        ]
    )
    assert codebase_scans(source) == {
        "test_direct": False,
        "test_through_a_helper": True,
        "test_walk": False,
    }
