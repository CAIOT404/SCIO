"""PreToolUse hook for Edit, Write and NotebookEdit: a session working in a worktree never
writes the main checkout's own files (I-226).

The main checkout holds the same tree as every worktree. An absolute path typed by hand can
drop the worktree's segment (``.claude/worktrees/<name>``) and still name a file that exists,
so the edit succeeds on the wrong copy: the main checkout goes dirty on ``main`` and the
worktree misses the change. This hook refuses that write and names the worktree's copy.

Allowed:

- a session whose ``cwd`` is the main checkout, or no repository at all;
- a path outside the main checkout (the scratchpad, the memory directory);
- a path in any worktree, the session's own or another's;
- a path git ignores in the main checkout (``.scio/``: approvals, agent reports).

To write the main checkout on purpose, run the session from it (``cd`` there first). Reads the
hook JSON (``tool_input.file_path`` or ``notebook_path``, ``cwd``) from stdin and prints a deny
decision as JSON; anything else is allowed silently. Any error allows the write: a broken guard
must not stop all work. A denial is logged like the push gate's (W-18). Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_gate  # a sibling script, not a package

RULE = "worktree-guard"


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
    )


def _real(path: str) -> str:
    return os.path.realpath(push_gate.native_dir(path))


def _norm(path: str) -> str:
    return os.path.normcase(_real(path))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def reason(file_path: str, cwd: str) -> str | None:
    """Why a write to ``file_path`` from a session in ``cwd`` is refused, or None."""
    cwd = push_gate.native_dir(cwd)
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if top.returncode or common.returncode:
        return None  # not a repository
    worktree = _norm(top.stdout.strip())
    main_dir = _real(os.path.dirname(common.stdout.strip().rstrip("/\\")))
    main = os.path.normcase(main_dir)
    target = _norm(file_path)
    if worktree == main or not _inside(target, main) or _inside(target, worktree):
        return None
    rel = os.path.relpath(_real(file_path), main_dir)  # keeps the path's own case
    if _inside(target, os.path.join(main, ".claude", "worktrees")):
        return None  # another worktree: an agent's, which the orchestrator may edit
    if _git(["check-ignore", "-q", "--", rel], main).returncode == 0:
        return None  # .scio/ and the like: the main checkout's own state, not its tree
    home = os.path.normpath(top.stdout.strip())
    return (
        f"This session works in the worktree {home}, and {file_path} is in the main checkout's "
        f"tree (I-226). Write {os.path.join(home, rel)} instead. To change the main checkout on "
        "purpose, run the session from it (cd there first)."
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") or {}
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        cwd = str(data.get("cwd") or os.getcwd())
        why = reason(path, cwd) if path else None
    except Exception:  # a broken guard allows the write
        return 0
    if why:
        push_gate.log_refusal(RULE, path, cwd)
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": why,
        }
        print(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
