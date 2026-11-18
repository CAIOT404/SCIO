"""Issue #104: the oldest open ledger entries, I-145 and I-159 (I-170's guards live beside the
waits they cover, in ``tests/live/test_dashboard_page.py``).

I-145: two product tests failed once in a parallel run, and their failures did not say why
the build halted. Their assertions now print the build's ``exit_reason`` and its last ledger
lines, or the trial's notes, error and time. The test here makes the GitHub-mode build test
fail on a fake halt and reads its failure text. The eval test's failure already showed its
notes, error and time (pytest prints the ``TrialResult``), so a fake halt there tells the old
message from the new one by nothing; what it lacks is the build's ledger, which the solver's
notes do not carry.

I-159: plan drift flagged a file the explorer named when the toolchain did not call it
source, such as a Python product's ``graphcalc/static/app.js``. A tracked file the explorer
named is now expected for its step unless it is under ``.scio/``, a docs file, or a
dependency or lock file. The tracked list below is the graphcalc product's, from
``tests/unit/test_localize_files.py``, with the dependency and lock files the rule names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.graph.test_product_build_fake as build_test
from scio.graph.nodes.planning import add_localized_files, localized_files
from scio.loop.escalation import drifted_files
from scio.product.build import BuildOutcome
from scio.state.models import (
    Budget,
    ExplorerSummary,
    LedgerEntry,
    Plan,
    PlanStep,
    ProductBuildState,
    Role,
)
from scio.toolchain import get_toolchain

PY = get_toolchain("python")
ROOT = "/work/graphcalc"
TRACKED = sorted(
    [
        ".scio/product/progress.json",
        "CHANGELOG.md",
        "PRODUCT.md",
        "README.md",
        "docs/notes.md",
        "graphcalc/__init__.py",
        "graphcalc/calc.py",
        "graphcalc/cli.py",
        "graphcalc/static/app.js",
        "graphcalc/static/index.html",
        "graphcalc/static/style.css",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        "requirements-dev.txt",
        "tests/test_static.py",
        "uv.lock",
    ]
)
HALT_FACT = "load: the product environment was not created: python -m venv timed out after 300 s"


# ---------- I-145: a failed build names its halt reason ----------


class _ClosedContext:
    def close(self) -> None:
        return None


def test_a_build_failure_names_its_halt_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GitHub-mode build test, its bootstrap and build replaced by a fake halt
    (``load_failed``, as in I-145): its failure text holds the exit reason and the ledger's
    last fact, so a recurrence in a parallel run names its cause."""
    halted = BuildOutcome(
        state=ProductBuildState(
            thread_id="build-todo_cli-m1",
            product_dir=str(tmp_path / "product"),
            phase="halted",
            exit_reason="load_failed",
            ledger=[
                LedgerEntry(kind="fact", text="load: PRODUCT.md read", source=Role.SYSTEM),
                LedgerEntry(kind="fact", text=HALT_FACT, source=Role.SYSTEM),
            ],
            budgets=Budget(tokens_max=1_000, usd_max=1.0, advisor_calls_max=1),
        ),
        interrupt=None,
    )
    monkeypatch.setattr(build_test, "_bootstrap", lambda _tmp: None)
    monkeypatch.setattr(build_test, "_git", lambda *_args: "")
    monkeypatch.setattr(build_test, "_ctx", lambda *_args, **_kw: _ClosedContext())
    monkeypatch.setattr(build_test, "run_build", lambda _ctx, **_kw: halted)
    with pytest.raises(AssertionError) as failed:
        build_test.test_github_mode_crash_after_the_pr_duplicates_nothing(tmp_path)
    text = str(failed.value)
    assert "exit_reason=load_failed" in text, text
    assert HALT_FACT in text, text


# ---------- I-159: which files the explorer named count as expected ----------


def _summary(findings: list[str], files: list[str]) -> ExplorerSummary:
    return ExplorerSummary(
        question="For plan step S1: where does the plot's zoom live?",
        findings=findings,
        relevant_files=files,
        open_questions=[],
    )


def _plan(files_expected: list[str]) -> Plan:
    return Plan(
        author=Role.PLANNER,
        rationale="a T1 UI step of a Python product",
        steps=[
            PlanStep(
                id="S1",
                description="zoom the plot",
                feature_ids=["F1"],
                files_expected=files_expected,
            )
        ],
    )


def test_a_static_asset_the_explorer_named_is_expected() -> None:
    """The python profile's source is ``.py``: before I-159 the explorer's
    ``graphcalc/static/app.js`` was filtered out, and the coder's edit to it drifted."""
    summary = _summary(
        ["the plot's zoom handler is graphcalc/static/app.js:120 (onWheel)"],
        ["graphcalc/cli.py", "graphcalc/static/app.js"],
    )
    # the toolchain's filter is passed and no longer decides
    files = localized_files(summary, tracked=TRACKED, is_source=PY.is_source, root=ROOT)
    assert files == ["graphcalc/cli.py", "graphcalc/static/app.js"]
    plan, added = add_localized_files(_plan(["graphcalc/cli.py"]), files, root=ROOT)
    assert added == {"S1": ["graphcalc/static/app.js"]}
    expected = plan.steps[0].files_expected
    touched = [f"{ROOT}/graphcalc/static/app.js", f"{ROOT}/graphcalc/cli.py"]
    assert drifted_files(touched, expected, root=ROOT, test_dirs=PY.test_dirs) == []
    # a file nobody named still drifts
    unnamed = [*touched, f"{ROOT}/graphcalc/static/style.css"]
    assert drifted_files(unnamed, expected, root=ROOT, test_dirs=PY.test_dirs) == [
        "graphcalc/static/style.css"
    ]


def test_docs_and_dependency_files_stay_out_of_expected_files() -> None:
    """Docs, SCIO's bookkeeping and the dependency and lock files are context the explorer
    read, never files its step may change unflagged: named, they still are not expected."""
    left_out = [
        "PRODUCT.md",
        "README.md",
        "CHANGELOG.md",
        "docs/notes.md",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "requirements-dev.txt",
        ".scio/product/progress.json",
    ]
    summary = _summary(
        [
            "PRODUCT.md:46-49 pins the zoom; progress.json:5 shows P1 done",
            "pyproject.toml:12 pins no plotting library",
        ],
        ["graphcalc/static/app.js", *left_out],
    )
    files = localized_files(summary, tracked=TRACKED, is_source=PY.is_source, root=ROOT)
    assert files == ["graphcalc/static/app.js"]
    plan, _ = add_localized_files(_plan(["graphcalc/cli.py"]), files, root=ROOT)
    expected = plan.steps[0].files_expected
    assert expected == ["graphcalc/cli.py", "graphcalc/static/app.js"]
    touched = [f"{ROOT}/{f}" for f in ("graphcalc/static/app.js", "PRODUCT.md", "pyproject.toml")]
    assert drifted_files(touched, expected, root=ROOT, test_dirs=PY.test_dirs) == [
        "PRODUCT.md",
        "pyproject.toml",
    ]
