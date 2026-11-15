"""Whether a CI run must run the regression evals (issue #122, W-36).

    python scripts/ci_changes.py        # in CI's `changes` job; EVENT and BASE from the workflow

The evals exercise the framework, so a pull request that changes none of it (docs, tests,
scripts) skips them by the `evals` job's own `if:`, never by a workflow `paths:` filter: a
workflow skipped by its filter leaves the required `checks` status pending forever. The
answer fails open. A push to main, the merge queue, any other event, and a diff that cannot
be read all run the evals; only a pull request whose diff touches no code path skips them.
Writes ``code=true`` or ``code=false`` to ``$GITHUB_OUTPUT`` when it is set.
"""

from __future__ import annotations

import os
import subprocess
import sys

CODE_DIRS = ("scio/", "evals/", "configs/", "prompts/", ".github/workflows/")
CODE_FILES = ("pyproject.toml", "uv.lock")


def touches_code(path: str) -> bool:
    """A changed path the evals depend on: the framework, its configs, or CI itself."""
    return path.startswith(CODE_DIRS) or path in CODE_FILES


def run_evals(event: str, changed: list[str] | None) -> bool:
    """Only a pull request with a readable diff that touches no code path skips the evals."""
    if event != "pull_request" or changed is None:
        return True
    return any(touches_code(p) for p in changed)


def changed_files(base: str) -> list[str] | None:
    """The files that differ between the pull request's base and the checked-out merge
    commit, or None when the diff cannot be read (no base, a commit not fetched). A rename
    lists both its paths, so moving a file out of `scio/` still counts as touching it."""
    if not base:
        return None
    argv = ["git", "-c", "core.quotePath=false", "diff", "--name-only", "--no-renames"]
    proc = subprocess.run([*argv, base, "HEAD"], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return None
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def main() -> int:
    event = os.environ.get("EVENT", "")
    changed = changed_files(os.environ.get("BASE", "")) if event == "pull_request" else None
    code = run_evals(event, changed)
    shown = "unread" if changed is None else f"{len(changed)} changed"
    print(f"event: {event or '?'}, diff: {shown}, run the evals: {code}")
    for path in changed or []:
        print(f"  {'code ' if touches_code(path) else 'other'} {path}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"code={'true' if code else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
