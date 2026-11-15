"""Shared fixtures for the session tools' tests (issue #122, W-20 to W-29).

Git runs for real, in repositories under ``tmp_path``: ``hub`` stands for GitHub (a plain,
non-bare repository, so a fake ``gh pr update-branch`` or a merge is made right there and the
tool's real ``git fetch`` picks it up) and ``SCIO`` is the owner's main checkout, cloned from
it. Nothing is ever pushed: the fake runner answers ``gh``, records ``git push``, and answers
the long-running scripts (``watch_ci.py``, ``python -m recovery status``, ``gate.py``,
``session_state.py``) from the test's own handlers.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from scio.settings import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
LEDGER = "docs/ISSUE_LEDGER.md"
LESSONS = "docs/LESSONS_LEARNED.md"
IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def load(name: str) -> ModuleType:
    """A script under ``scripts/``, loaded by its path (they are not a package), registered in
    ``sys.modules`` for ``@dataclass`` (I-174)."""
    path = (SCRIPTS / f"{name}.py").resolve()
    key = f"_session_tools_{name}_{abs(hash(str(path)))}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *IDENTITY, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def tree(repo: Path, rev: str = "HEAD") -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}")


def ledger(last: int) -> str:
    """A ledger whose entries run I-001 to I-<last>; each body mentions I-099, which is not an
    entry and must not count."""
    entries = "".join(
        f"### I-{i:03d} · entry {i}\n- Seen with I-099 in mind.\n- Resolved: - · Status: open\n\n"
        for i in range(1, last + 1)
    )
    return f"# Issue ledger\n\n{entries}"


def lessons(last: int) -> str:
    """LESSONS_LEARNED with rules 1 to <last>, then a retrospective whose numbered list (40.)
    must not count as a guardrail."""
    rules = "".join(
        f"{i}. **Always** rule {i} (I-{i:03d}). Guard: `manual:` x\n" for i in range(1, last + 1)
    )
    return (
        "# Lessons learned\n\n## Guardrails (Always / Never)\n\n"
        f"{rules}\n## M0 retrospective\n\n40. **Always** a list item, not a rule\n"
    )


def make_hub(tmp: Path, *, ledger_last: int = 5, guard_last: int = 3) -> Path:
    """ "GitHub": a repository on ``main`` with the docs the tools read and ``.scio/`` ignored."""
    hub = tmp / "hub"
    hub.mkdir()
    git(hub, "init", "-q", "-b", "main")
    commit(
        hub,
        {
            ".gitignore": ".scio/\n",
            "README.md": "# SCIO\n",
            LEDGER: ledger(ledger_last),
            LESSONS: lessons(guard_last),
        },
        "base",
    )
    return hub


def clone(hub: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(hub), str(dest)], check=True, capture_output=True)
    return dest


def branch_commit(hub: Path, branch: str, files: dict[str, str], *, start: str = "main") -> str:
    """Commit ``files`` on ``branch`` (made from ``start`` when new) in the hub, then return the
    hub to ``main``."""
    exists = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
    )
    if exists.returncode == 0:
        git(hub, "checkout", "-q", branch)
    else:
        git(hub, "checkout", "-q", "-b", branch, start)
    sha = commit(hub, files, f"work on {branch}")
    git(hub, "checkout", "-q", "main")
    return sha


def merge_into(hub: Path, target: str, source: str) -> str:
    """A merge commit of ``source`` into ``target`` in the hub (GitHub's merge button, or
    ``gh pr update-branch`` when ``source`` is main)."""
    git(hub, "checkout", "-q", target)
    git(hub, "merge", "-q", "--no-ff", source, "-m", f"Merge {source} into {target}")
    sha = git(hub, "rev-parse", "HEAD")
    git(hub, "checkout", "-q", "main")
    return sha


def stamp(
    worktree: Path, tree_id: str, *, mode: str = "quick", head: str = "", **extra: Any
) -> None:
    """The gate's pass stamp (the gate v2 contract), as ``scripts/gate.py`` writes it."""
    data = {
        "tree": tree_id,
        "head": head,
        "mode": mode,
        "at": extra.pop("at", "2026-11-14T12:00:00Z"),
        "counts": extra.pop("counts", {"pytest": "10 passed"}),
    }
    write(worktree, {".scio/gate/last-pass.json": json.dumps(data)})


GhHandler = Callable[[list[str]], tuple[int, str]]
PyHandler = Callable[[list[str], Path | None], tuple[int, str]]


class FakeRunner:
    """The session tools' runner with GitHub and the slow scripts faked; git runs for real
    except ``git push``, which is only recorded."""

    def __init__(self, gh: GhHandler | None = None, python: PyHandler | None = None) -> None:
        self.gh = gh or (lambda args: (1, f"no fake for gh {' '.join(args)}"))
        self.python = python or (lambda argv, cwd: (0, ""))
        self.calls: list[list[str]] = []
        self.events: list[str] = []
        self.pushes: list[list[str]] = []
        self.spawned: list[tuple[list[str], Path, Path, dict[str, str] | None]] = []
        self.sleeps: list[float] = []
        self.clock = 0.0
        self._real = load("session_state").Runner()

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        *,
        input: str | None = None,
        timeout: float | None = 120.0,
        stream: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        self.calls.append(args)
        if args[0] == "gh":
            code, out = self.gh(args[1:])
            self.events.append("gh " + " ".join(args[1:3]))
            return subprocess.CompletedProcess(args, code, out if code == 0 else "", out)
        if args[0] == "git" and "push" in args:
            self.pushes.append(args)
            self.events.append("git push")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == sys.executable:
            code, out = self.python(args[1:], cwd)
            self.events.append("python " + _script_name(args[1:]))
            return subprocess.CompletedProcess(args, code, out, "" if code == 0 else out)
        real: subprocess.CompletedProcess[str] = self._real.run(
            args, cwd, input=input, timeout=timeout
        )
        return real

    def spawn(
        self, argv: Sequence[str], cwd: Path, log: Path, env: dict[str, str] | None = None
    ) -> int:
        self.spawned.append(([str(a) for a in argv], cwd, log, env))
        self.events.append("spawn")
        return 4242

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def now(self) -> float:
        return self.clock

    def gh_calls(self) -> list[list[str]]:
        return [c[1:] for c in self.calls if c[0] == "gh"]


def _script_name(argv: list[str]) -> str:
    if argv[:2] == ["-m", "recovery"]:
        return "recovery " + " ".join(argv[2:3])
    return Path(argv[0]).name if argv else ""


def same(a: Path | str, b: Path | str) -> bool:
    return Path(a).resolve() == Path(b).resolve()


WORKFLOW_DOC = """# Workflow v2: the checklist

## Process rules

### W-01 · A closed item
- From: a test.
- Change: README exists.
- PR: none
- Evidence: `file:README.md`

### W-02 · An open item
- From: a test.
- Change: a file that is not there yet.
- PR: none
- Evidence: `file:docs/not-yet.md`
"""

WORKFLOW_STUB = r'''"""A stand-in for scripts/workflow_status.py and its report(), used until
that script is on this branch (issue #122)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CLOSED = ("done", "superseded", "dropped")


@dataclass
class Item:
    id: str
    title: str


def report(root):
    text = (Path(root) / "docs" / "WORKFLOW_V2.md").read_text(encoding="utf-8")
    rows = []
    for block in re.split(r"(?m)^(?=### W-)", text)[1:]:
        head, _, body = block.partition("\n")
        wid, _, title = head[4:].partition(" · ")
        files = re.findall(r"`file:([^`]+)`", body)
        done = bool(files) and all((Path(root) / f).is_file() for f in files)
        rows.append((Item(wid, title.strip()), "done" if done else "open"))
    return rows, []
'''


def add_workflow_checklist(repo: Path) -> None:
    """``docs/WORKFLOW_V2.md`` with one closed and one open item, and
    ``scripts/workflow_status.py``: the real one when this branch has it, else the stub."""
    real = SCRIPTS / "workflow_status.py"
    write(repo, {"docs/WORKFLOW_V2.md": WORKFLOW_DOC})
    target = repo / "scripts" / "workflow_status.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    if real.is_file():
        shutil.copyfile(real, target)
    else:
        write(repo, {"scripts/workflow_status.py": WORKFLOW_STUB})
