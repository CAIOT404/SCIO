"""`scripts/watch_ci.py` (issue #88): the run it waits for is the pushed head's own."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_ci", REPO_ROOT / "scripts" / "watch_ci.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_watcher_takes_the_pushed_heads_run_never_the_previous_green_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-104's mistake: right after a push the newest finished run on the branch is the
    previous head's, green, and `gh pr checks --watch` reported it (or "no checks" and
    exited). The watcher matches the run by the pushed SHA, and waits while there is none."""
    wc = _load()
    rows = [
        {"databaseId": 2, "headSha": "old", "status": "completed", "conclusion": "success"},
        {"databaseId": 1, "headSha": "older", "status": "completed", "conclusion": "failure"},
    ]
    monkeypatch.setattr(wc, "_out", lambda argv: json.dumps(rows))
    assert wc.run_for_head("feat/x", "new", "ci") is None  # not registered yet: keep waiting
    rows.insert(0, {"databaseId": 3, "headSha": "new", "status": "queued", "conclusion": ""})
    run = wc.run_for_head("feat/x", "new", "ci")
    assert run is not None and run["databaseId"] == 3


def test_a_short_sha_is_resolved_to_the_full_one_github_reports() -> None:
    """I-172: `--sha 6dba0d2` never matched GitHub's full `headSha`, so the watcher waited out
    its 300 s and gave up on a run that was in progress."""
    wc = _load()
    full = wc.full_sha(None)
    assert len(full) == 40 and wc.full_sha(full[:7]) == full


def test_a_full_sha_made_on_github_is_taken_as_given() -> None:
    """I-199: `gh pr update-branch` makes the merge commit on GitHub, and the watcher
    crashed resolving it here (`git rev-parse` exit 128). A full SHA is now taken as
    given; it never has to exist in this clone."""
    wc = _load()
    remote_only = "AB" * 20
    assert wc.full_sha(remote_only) == remote_only.lower()


def test_an_unresolvable_short_sha_ends_with_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wc = _load()
    monkeypatch.setattr("sys.argv", ["watch_ci.py", "--sha", "0000000"])
    assert wc.main() == 2
    assert "cannot resolve 0000000" in capsys.readouterr().err


def test_the_watcher_lists_runs_of_one_workflow_on_one_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wc = _load()
    seen: list[list[str]] = []

    def out(argv: list[str]) -> str:
        seen.append(argv)
        return "[]"

    monkeypatch.setattr(wc, "_out", out)
    wc.run_for_head("feat/x", "abc", "ci")
    argv = seen[0]
    assert argv[:3] == ["gh", "run", "list"]
    assert argv[argv.index("--branch") + 1] == "feat/x"
    assert argv[argv.index("--workflow") + 1] == "ci"
    assert Path(REPO_ROOT / "scripts" / "watch_ci.py").is_file()


def test_without_a_branch_the_run_is_found_by_its_commit_not_the_checkouts_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-211: `watch_ci.py --sha <#117's head>` run from the main checkout filtered runs by
    `--branch main`, the checkout's own branch, and gave up after 300 s on a run that was in
    progress on `feat/115-budget-metric`. With no `--branch`, the run is looked up by its
    commit, wherever it was pushed."""
    wc = _load()
    head = "cd" * 20
    seen: list[list[str]] = []

    def out(argv: list[str]) -> str:
        seen.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return json.dumps([{"databaseId": 7, "headSha": head, "url": "u"}])
        if argv[:3] == ["gh", "run", "view"]:
            return json.dumps({"conclusion": "success", "jobs": []})
        raise AssertionError(f"unexpected command: {argv}")  # never `git rev-parse` for a branch

    class Done:
        returncode = 0

    monkeypatch.setattr(wc, "_out", out)
    monkeypatch.setattr(wc.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr("sys.argv", ["watch_ci.py", "--sha", head])
    assert wc.main() == 0
    listed = seen[0]
    assert "--branch" not in listed and listed[listed.index("--commit") + 1] == head
    assert "by commit" in capsys.readouterr().out
