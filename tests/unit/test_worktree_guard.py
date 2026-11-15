"""The worktree guard (I-226): a session working in a worktree never writes the main checkout's
own files, and everything else it writes goes through."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scio.settings import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "hooks" / "worktree_guard.py"


@pytest.fixture(autouse=True)
def machine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The refusal log (W-18) goes to this test's own directory, not the machine's."""
    machine = tmp_path / "machine"
    monkeypatch.setenv("LOCALAPPDATA", str(machine))
    monkeypatch.setenv("XDG_CACHE_HOME", str(machine))
    return machine


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout on ``main`` with a worktree under ``.claude/worktrees/w``."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "README.md").write_text("one\n", encoding="utf-8", newline="\n")
    (main / ".gitignore").write_text(".scio/\n", encoding="utf-8", newline="\n")
    _git("add", ".", cwd=main)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one", cwd=main)
    worktree = main / ".claude" / "worktrees" / "w"
    _git("worktree", "add", "-q", "-b", "w", str(worktree), cwd=main)
    return main, worktree


def _hook(path: Path, cwd: Path, tool: str = "Edit") -> str | None:
    """The deny reason the hook prints for one write, or None when it allows it."""
    payload = {"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(cwd)}
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not out.strip():
        return None
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return str(decision["permissionDecisionReason"])


def test_a_worktree_session_cannot_write_the_main_checkouts_files(
    repo: tuple[Path, Path], machine_dir: Path
) -> None:
    main, worktree = repo
    denied = _hook(main / "README.md", worktree)
    assert denied is not None and "I-226" in denied
    assert os.path.join(".claude", "worktrees", "w", "README.md") in denied  # the right copy
    assert _hook(main / "new_module.py", worktree, tool="Write") is not None  # a new file too
    log = next(machine_dir.rglob("hook-refusals.log")).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["rule"] for line in log] == ["worktree-guard", "worktree-guard"]


def test_the_worktree_guard_allows_every_other_write(
    repo: tuple[Path, Path], tmp_path: Path
) -> None:
    main, worktree = repo
    assert _hook(worktree / "README.md", worktree) is None  # the session's own copy
    assert _hook(main / ".scio" / "approvals.md", worktree) is None  # ignored state
    assert _hook(tmp_path / "scratch.py", worktree) is None  # outside the repository
    assert _hook(main / "README.md", main) is None  # a session in the main checkout
    assert _hook(main / "README.md", tmp_path) is None  # a session in no repository


def test_a_broken_payload_allows_the_write() -> None:
    out = subprocess.run(
        [sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True, check=True
    )
    assert out.stdout == ""
