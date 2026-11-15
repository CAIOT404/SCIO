"""CI's own workflow (issue #122, W-35 to W-37): the test shards hold every test file once, the
evals skip only by a job condition the required `checks` accepts, and the merge queue gets a
`checks` status."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from scio.settings import REPO_ROOT

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CODE_PATHS = (
    "scio/cli.py",
    "evals/runner.py",
    "configs/models.yaml",
    "prompts/plan.md",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
)
OTHER_PATHS = ["docs/WORKFLOW_V2.md", "README.md", "tests/unit/test_gate.py", "scripts/gate.py"]


def _workflow() -> dict[str, Any]:
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(wf, dict)
    return wf


def _triggers(wf: dict[str, Any]) -> dict[str, Any]:
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on:` key as the boolean True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    assert isinstance(on, dict), on
    return on


def _names(needs: str | list[str] | None) -> list[str]:
    return [needs] if isinstance(needs, str) else list(needs or [])


def _load_changes() -> ModuleType:
    path = REPO_ROOT / "scripts" / "ci_changes.py"
    spec = importlib.util.spec_from_file_location("ci_changes", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _test_files() -> set[str]:
    """Every file pytest collects under tests/ (its default `python_files`)."""
    root = REPO_ROOT / "tests"
    return {
        p.relative_to(REPO_ROOT).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for p in root.rglob(pattern)
        if "__pycache__" not in p.parts
    }


def _expand(path: str, every: set[str]) -> set[str]:
    """The test files pytest runs for one shard path: a file, or every test file below a
    directory."""
    target = REPO_ROOT / path
    if target.is_dir():
        found = {f for f in every if f.startswith(path.rstrip("/") + "/")}
        assert found, f"shard path {path} holds no test file"
        return found
    assert target.is_file() and path in every, f"shard path {path} is no test file under tests/"
    return {path}


def test_ci_shards_cover_every_test_directory() -> None:
    """W-35: the tests job is a matrix of shards whose paths together hold every test file
    under tests/ exactly once, and the pytest step runs the shard's own paths in every shard.
    tests/live is in a shard too: CI's tests job has always collected it (the dashboard's
    browser tests, the docker tests on the plain image), so leaving it out would drop tests."""
    wf = _workflow()
    tests = wf["jobs"]["tests"]
    assert "strategy" in tests, "the tests job is a single job, not a matrix of shards"
    include = tests["strategy"]["matrix"]["include"]
    shards = {int(entry["shard"]): str(entry["paths"]).split() for entry in include}
    assert sorted(shards) == list(range(1, len(shards) + 1)) and 2 <= len(shards) <= 3, shards

    every = _test_files()
    placed: dict[str, int] = {}
    for k, paths in sorted(shards.items()):
        for f in sorted(set().union(*(_expand(p, every) for p in paths))):
            assert f not in placed, f"{f} is in shard {placed[f]} and in shard {k}"
            placed[f] = k
    not_live = {f for f in every if not f.startswith("tests/live/")}
    missing = sorted(not_live - set(placed))
    assert not missing, f"in no CI shard (add each to a shard in ci.yml): {missing}"
    assert set(placed) == every, sorted(every - set(placed))

    # Every shard runs, and its pytest step runs that shard's paths, not the whole suite.
    assert "if" not in tests and tests["strategy"].get("fail-fast") is False
    step = next(s for s in tests["steps"] if "pytest" in str(s.get("run", "")))
    assert "if" not in step
    assert step["env"]["SHARD_PATHS"] == "${{ matrix.paths }}"
    assert "uv run pytest $SHARD_PATHS " in step["run"]


def _checks_verdict(
    script: Path,
    *,
    changes: str = "success",
    tests: str = "success",
    evals: str = "success",
    code: str = "true",
) -> int:
    """Run `checks`'s step as GitHub runs a `shell: python` step (`python {0}`), with the
    `needs` context it would see."""
    needs = {
        "changes": {"result": changes, "outputs": {"code": code} if changes == "success" else {}},
        "tests": {"result": tests, "outputs": {}},
        "evals": {"result": evals, "outputs": {}},
    }
    env = {**os.environ, "NEEDS": json.dumps(needs)}
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode


def test_ci_skips_evals_only_by_a_job_condition_and_checks_accepts_it(tmp_path: Path) -> None:
    """W-36: no workflow `paths:` filter (a workflow skipped by one leaves the required `checks`
    pending); the evals job has a condition on the change detection's output; `checks` runs
    always, needs every shard and the evals, fails on a failed or cancelled job, and accepts
    skipped evals only when the change detection found no code changed."""
    wf = _workflow()
    for event, config in _triggers(wf).items():
        filters = {"paths", "paths-ignore"} & set(config or {})
        assert not filters, f"{event} has a workflow-level {sorted(filters)} filter"

    jobs = wf["jobs"]
    assert "changes" in jobs, "no change-detection job"
    assert jobs["changes"]["outputs"]["code"] == "${{ steps.diff.outputs.code }}"
    evals = jobs["evals"]
    assert "changes" in _names(evals.get("needs"))
    assert "needs.changes.outputs.code == 'true'" in str(evals.get("if", ""))

    checks = jobs["checks"]
    assert checks.get("if") in ("always()", "${{ always() }}"), "a skipped `checks` reads as passed"
    assert {"changes", "tests", "evals"} <= set(_names(checks["needs"]))
    matrices = [name for name, job in jobs.items() if "matrix" in job.get("strategy", {})]
    assert matrices == ["tests"], "every shard is a leg of the one `tests` matrix"
    (step,) = checks["steps"]
    assert step["shell"] == "python" and step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    script = tmp_path / "checks.py"
    script.write_text(step["run"], encoding="utf-8", newline="\n")

    assert _checks_verdict(script) == 0
    assert _checks_verdict(script, evals="skipped", code="false") == 0
    for bad in ("failure", "cancelled"):
        assert _checks_verdict(script, tests=bad) == 1
        assert _checks_verdict(script, evals=bad) == 1
        assert _checks_verdict(script, evals=bad, code="false") == 1
        assert _checks_verdict(script, changes=bad, evals="skipped") == 1
    assert _checks_verdict(script, tests="skipped") == 1  # the shards always run
    assert _checks_verdict(script, evals="skipped", code="true") == 1  # skipped, not cleared

    changes = _load_changes()
    assert changes.run_evals("pull_request", OTHER_PATHS) is False
    for path in CODE_PATHS:
        assert changes.run_evals("pull_request", [*OTHER_PATHS, path]) is True, path
    assert changes.run_evals("pull_request", None) is True  # an unreadable diff runs them


def test_ci_runs_on_the_merge_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W-37: `merge_group` is a trigger, so a queued group gets the required `checks` status;
    there the change detection runs everything, as on a push to main."""
    wf = _workflow()
    triggers = _triggers(wf)
    assert "merge_group" in triggers, sorted(triggers)
    assert triggers["merge_group"] == {"types": ["checks_requested"]}

    jobs = wf["jobs"]
    assert "github.event_name != 'pull_request'" in str(jobs["evals"]["if"])
    for name in ("checks", "changes", "tests"):
        assert "event_name" not in str(jobs[name].get("if", "")), name
    step = next(s for s in jobs["changes"]["steps"] if s.get("id") == "diff")
    assert "scripts/ci_changes.py" in step["run"]
    assert step["env"]["EVENT"] == "${{ github.event_name }}"

    changes = _load_changes()
    for event in ("merge_group", "push"):
        assert changes.run_evals(event, None) is True
        assert changes.run_evals(event, OTHER_PATHS) is True
    out = tmp_path / "github_output"
    monkeypatch.setenv("EVENT", "merge_group")
    monkeypatch.setenv("BASE", "")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert changes.main() == 0
    assert out.read_text(encoding="utf-8") == "code=true\n"


def _git(repo: Path, *args: str) -> str:
    who = ["-c", "user.name=ci", "-c", "user.email=ci@example.invalid"]
    proc = subprocess.run(["git", *who, *args], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_the_change_detection_counts_a_file_moved_out_of_scio_as_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff is read without rename detection: a file moved from scio/ to docs/ lists both
    paths, so the evals run; a docs-only change skips them; a base that is no commit runs them."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "scio").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "scio" / "mod.py").write_text("X = 1\n" * 20, encoding="utf-8", newline="\n")
    (tmp_path / "docs" / "a.md").write_text("a\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    changes = _load_changes()

    (tmp_path / "docs" / "a.md").write_text("b\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "commit", "-q", "-am", "docs only")
    assert changes.changed_files(base) == ["docs/a.md"]
    assert changes.run_evals("pull_request", changes.changed_files(base)) is False

    _git(tmp_path, "mv", "scio/mod.py", "docs/mod.py")
    _git(tmp_path, "commit", "-q", "-m", "move")
    assert sorted(changes.changed_files(base)) == ["docs/a.md", "docs/mod.py", "scio/mod.py"]
    assert changes.run_evals("pull_request", changes.changed_files(base)) is True

    assert changes.changed_files("0" * 40) is None
    assert changes.changed_files("") is None
