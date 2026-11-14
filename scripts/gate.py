"""The local CI-equivalent (CLAUDE.md): quick by default, full on demand (issues #88, #122).

    uv run python scripts/gate.py           # quick: lint, types, the changed paths' tests, docs
    uv run python scripts/gate.py --full    # everything CI runs, as the gate always did

``--full`` is for a release cut, after a red CI, and for a change to the gate or the push-gate
hook. It runs the same checks and thresholds as CI's ``checks`` and ``evals`` jobs:
1. ``ruff check``, ``ruff format --check``, ``mypy``, in order, stopping at the first failure;
2. ``pytest -n 16`` (the owner's decision, 2026-11-08) and the regression evals (5 trials,
   pass^5 = 1.00, trials run concurrently) at the same time: the tests wait on git and pytest
   subprocesses, the evals on the Fake, and the machine has cores to spare for both;
3. the docs check.

Quick (W-14) runs step 1, then the tests of the paths changed since the last pass (the stamp's
tree, or the merge base with ``origin/main`` when there is no stamp), then the docs check. A
change to ``conftest.py``, ``pyproject.toml``, ``uv.lock``, ``configs/``, ``prompts/`` or
``tests/fixtures/``, or a changed file no test mentions, runs the whole suite; the gate says
which. The evals run only when ``scio/``, ``evals/``, ``configs/`` or ``prompts/`` changed (W-15).
When every changed path since the stamp is Markdown documentation, only the docs check and the
tests that read docs run (W-13, mode ``docs``). Markdown the code reads (prompt templates, eval
task specs) is code, not documentation.

A pass writes the stamp ``.scio/gate/last-pass.json`` (W-11): the tree it tested, HEAD, the
mode, the time and each step's verdict. The push-gate hook refuses to push a tree no stamp
holds (W-17), and a tree that already passed is not gated again (W-12). ``.scio/gate/RUNNING``
exists while a gate runs, so a gate killed hard leaves it behind. One gate runs at a time on the
machine (W-16): a second one waits for ``gate.lock`` under ``%LOCALAPPDATA%/SCIO``
(``~/.cache/scio`` elsewhere) and says whose gate it waits for.

Every Python tool runs as ``python -m`` with this interpreter: Application Control blocks the
venv's launcher ``.exe`` files on the dev machine (I-075, I-146). Each step's output goes to a
log file under ``.scio/gate/<UTC stamp>/``, never a pipe; the gate prints each step's wall time
and the tail of a failed step's log, and exits 1 if any step failed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TEST_WORKERS = 16
"""The owner's decision, 2026-11-08 (issue #88); 8 since issue #73."""
EVAL_PARALLEL = 5
"""Trials of a task run at once (``evals.runner --parallel``); the 5 trials and the pass^5
threshold are unchanged."""

FIRST: list[tuple[str, list[str]]] = [
    ("ruff check", [PY, "-m", "ruff", "check", "."]),
    ("ruff format", [PY, "-m", "ruff", "format", "--check", "."]),
    ("mypy", [PY, "-m", "mypy"]),
]
TOGETHER: list[tuple[str, list[str]]] = [
    ("pytest", [PY, "-m", "pytest", "-n", str(TEST_WORKERS)]),
    (
        "evals",
        [
            PY,
            "-m",
            "evals.runner",
            "--suite",
            "regression",
            "--provider",
            "fake",
            "--trials",
            "5",
            "--min-pass-pow-k",
            "1.0",
            "--parallel",
            str(EVAL_PARALLEL),
        ],
    ),
]
LAST: list[tuple[str, list[str]]] = [("docs", [PY, "scripts/check_docs.py"])]


def steps() -> list[tuple[str, list[str]]]:
    """Every step of the full gate, in the order the gate reports them."""
    return [*FIRST, *TOGETHER, *LAST]


SYNC = "UV_SYSTEM_CERTS=1 uv sync --frozen --all-extras --group dev"
"""CI's sync. A fresh worktree's first ``uv run`` installs the default groups only, and the
type check imports the extras (I-156, I-205)."""
_NAME_END = re.compile(r"[\s\[<>=!~;@]")


def missing_distributions(pyproject: Path) -> list[str]:
    """The distributions named by ``pyproject``'s optional extras and its ``dev`` group that
    this interpreter's environment lacks; none when there is no ``pyproject``. CI installs all
    of them, so a gate without them fails on code nobody changed (I-156, I-205).

    A requirement's environment marker is not evaluated: the name before it is wanted on every
    platform. None of SCIO's extras carries one today, and the test runs this against the real
    ``pyproject.toml`` on CI's runner, so a marker added later shows up there first."""
    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    wanted = [r for reqs in extras.values() for r in reqs]
    wanted += [r for r in data.get("dependency-groups", {}).get("dev", []) if isinstance(r, str)]
    missing: set[str] = set()
    for req in wanted:
        name = _NAME_END.split(req.strip(), maxsplit=1)[0]
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.add(name)
    return sorted(missing)


@dataclass
class Step:
    name: str
    code: int
    seconds: float
    log: Path


def _run(name: str, argv: list[str], logs: Path) -> Step:
    log = logs / f"{name.replace(' ', '-')}.log"
    start = time.monotonic()
    with log.open("wb") as out:
        code = subprocess.run(argv, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
    return Step(name, code, time.monotonic() - start, log)


VERDICT_MARKS = ("pass^", " passed", " failed", "problem(s)", "Success:", "Found ")
"""Lines that state a step's verdict (pytest's count, the evals' pass^k, mypy's), shown in
place of the log's last line when there is one."""


def _report(step: Step) -> str:
    """Print the step's verdict line, and a failed step's log tail; return the verdict."""
    lines = step.log.read_text(encoding="utf-8", errors="replace").splitlines()
    last = next(
        (ln.strip() for ln in reversed(lines) if any(m in ln for m in VERDICT_MARKS)),
        next((ln.strip() for ln in reversed(lines) if ln.strip()), ""),
    )
    verdict = "ok  " if step.code == 0 else "FAIL"
    print(
        f"{verdict} {step.name:<12} {step.seconds / 60:5.1f} min  exit {step.code}  {last[:120]}",
        flush=True,
    )
    if step.code != 0:
        print("\n".join(f"    {line}" for line in lines[-40:]), flush=True)
    return last[:200]


# --- The stamp and the RUNNING marker (W-11, W-12) -------------------------------------------

GATE_DIR = Path(".scio") / "gate"
"""Under the worktree root, git-ignored: the logs, the stamp and the RUNNING marker."""


class GitError(RuntimeError):
    """A git call failed; the gate then falls back to the safe side (full, no stamp)."""


GIT_ERRORS = (GitError, OSError, subprocess.TimeoutExpired)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def tested_tree() -> str:
    """The id of the tree the gate tests: HEAD's tree when the worktree is clean, else the tree
    that ``git add -A`` and a commit would record, built in a temporary index. So a gate run
    before the commit stamps the tree the commit then has, and a commit of only part of the
    changes does not match it."""
    if not _git("status", "--porcelain", "--untracked-files=all").strip():
        return _git("rev-parse", "HEAD^{tree}").strip()
    index = Path(_git("rev-parse", "--git-path", "index").strip())
    index = index if index.is_absolute() else ROOT / index
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        if index.is_file():
            shutil.copyfile(index, scratch)  # its stat cache spares hashing unchanged files
        else:
            _git("read-tree", "HEAD", env=env)
        _git("add", "-A", env=env)
        return _git("write-tree", env=env).strip()


def _tree_or_none() -> str | None:
    try:
        return tested_tree()
    except GIT_ERRORS:
        return None  # not a git work tree, or no commit yet: no stamp, no skip


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` with LF line endings through a temporary file and a rename, so a reader
    never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def stamp_path() -> Path:
    return ROOT / GATE_DIR / "last-pass.json"


def running_path() -> Path:
    return ROOT / GATE_DIR / "RUNNING"


def write_stamp(tree: str, head: str, mode: str, counts: dict[str, str]) -> Path:
    """W-11: record that ``tree`` passed the gate in ``mode`` (``full``, ``quick`` or ``docs``).

    The contract the push-gate hook, ``land.py`` and ``session_state.py`` read:
    ``{"tree", "head", "mode", "at", "counts"}`` in ``.scio/gate/last-pass.json``. Only a pass
    calls this; a failed gate leaves the previous stamp as it was."""
    path = stamp_path()
    _write_json(path, {"tree": tree, "head": head, "mode": mode, "at": _now(), "counts": counts})
    return path


def read_stamp() -> dict[str, Any] | None:
    stamp = _read_json(stamp_path())
    return stamp if stamp is not None and isinstance(stamp.get("tree"), str) else None


def _already_passed(tree: str | None, full: bool) -> bool:
    """W-12: the tree under test already passed, so there is nothing to gate. ``--full`` asks
    for more than a quick or docs pass gave, so only a full stamp satisfies it."""
    stamp = read_stamp()
    if tree is None or stamp is None or stamp["tree"] != tree:
        return False
    if full and stamp.get("mode") != "full":
        return False
    print(f"gate: tree {tree[:12]} already passed ({stamp.get('mode')}, {stamp.get('at')})")
    return True


def _mark_running(logs: Path) -> Path:
    """Write RUNNING. A marker left by a gate that died says so first: the last pass did not
    end in a verdict, and its logs are where it stopped."""
    marker = running_path()
    left = _read_json(marker)
    pid = left.get("pid") if left else None
    if left and isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
        print(
            f"gate: the gate that started here at {left.get('at')} (pid {pid}) died before it "
            f"finished; its logs: {left.get('logs')}",
            flush=True,
        )
    _write_json(marker, {"pid": os.getpid(), "at": _now(), "logs": str(logs)})
    return marker


def _clear_running(marker: Path) -> None:
    left = _read_json(marker)
    if left is None or left.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            marker.unlink()


# --- What changed, and which tests it maps to (W-13, W-14, W-15) -----------------------------

CODE_DIRS = ("scio/", "evals/", "configs/", "prompts/")
"""A change under these runs the evals (W-15)."""
WHOLE_SUITE_NAMES = {"conftest.py", "pyproject.toml", "uv.lock"}
WHOLE_SUITE_DIRS = ("configs/", "prompts/", "tests/fixtures/")
DOCS_TESTS = ("tests/unit/test_check_docs.py", "tests/unit/test_workflow_status.py")
"""With every test file whose name holds ``docs`` or ``guardrail``: the tests that read docs."""


def is_docs(path: str) -> bool:
    """A Markdown file that is documentation. A prompt template (``scio/prompt/templates``), an
    eval task's spec (``evals/tasks``) and a fixture are Markdown the code reads, so they are
    code here."""
    return path.endswith(".md") and not path.startswith((*CODE_DIRS, *WHOLE_SUITE_DIRS))


def change_base(stamp: dict[str, Any] | None) -> tuple[str, str, bool]:
    """``(base, how it was chosen, whether it is the stamp's tree)``: the last passed tree when
    git still has it, else the merge base with ``origin/main``. Raises when neither exists."""
    tree = stamp.get("tree") if stamp else None
    if isinstance(tree, str):
        try:
            _git("cat-file", "-e", f"{tree}^{{tree}}")
            return tree, f"since the stamp's tree {tree[:12]}", True
        except GIT_ERRORS:
            pass
    base = _git("merge-base", "origin/main", "HEAD").strip()
    return base, f"since origin/main ({base[:12]})", False


def changed_paths(base: str) -> list[str]:
    """Every path that differs between ``base`` and the working tree, untracked files included."""
    diff = _git("diff", "--name-only", "--no-renames", "-z", base, "--")
    new = _git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted({p for p in (diff + "\0" + new).split("\0") if p})


def all_test_files() -> list[str]:
    """Every test file under ``tests/``, outside ``tests/fixtures/``, relative to the root."""
    tests = ROOT / "tests"
    if not tests.is_dir():
        return []
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in tests.rglob("test_*.py")
        if p.relative_to(tests).parts[0] != "fixtures"
    )


def docs_tests() -> list[str]:
    found = {p for p in DOCS_TESTS if (ROOT / p).is_file()}
    for path in all_test_files():
        name = path.rsplit("/", 1)[-1]
        if "docs" in name or "guardrail" in name:
            found.add(path)
    return sorted(found)


def _word(text: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.-]){re.escape(text)}(?![\w-])")


def mention_patterns(path: str) -> list[re.Pattern[str]]:
    """How a test file mentions ``path``: a script by its file name or ``scripts/<stem>``; a
    module by its dotted name, its path, or ``from <package> import <module>``; any other file
    by its name."""
    name = path.rsplit("/", 1)[-1]
    if not path.endswith(".py"):
        return [_word(name)]
    stem = path.removesuffix(".py")
    if path.startswith("scripts/"):
        return [_word(name), _word(stem)]
    stem = stem.removesuffix("/__init__")
    dotted = stem.replace("/", ".")
    patterns = [_word(dotted), _word(stem)]
    if "." in dotted:
        package, module = dotted.rsplit(".", 1)
        patterns.append(
            re.compile(
                rf"from\s+{re.escape(package)}\s+import\s+(?:\([^)]*|[^\n]*)"
                rf"\b{re.escape(module)}\b"
            )
        )
    return patterns


def select_tests(changed: list[str]) -> tuple[list[str], str | None]:
    """``(test files, None)`` for the changed paths, or ``([], why)`` when the whole suite must
    run: a shared fixture or config changed, or a changed file maps to no test."""
    tests = all_test_files()
    texts: dict[str, str] = {}

    def text(test: str) -> str:
        if test not in texts:
            texts[test] = (ROOT / test).read_text(encoding="utf-8", errors="replace")
        return texts[test]

    selected: set[str] = set()
    for path in changed:
        name = path.rsplit("/", 1)[-1]
        if name in WHOLE_SUITE_NAMES or path.startswith(WHOLE_SUITE_DIRS):
            return [], f"{path} changed"
        if path.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
            if (ROOT / path).is_file():  # a deleted test file has nothing left to run
                selected.add(path)
            continue
        if is_docs(path):
            selected.update(docs_tests())
            continue
        patterns = mention_patterns(path)
        hits = [t for t in tests if any(p.search(text(t)) for p in patterns)]
        if not hits:
            return [], f"{path} maps to no test"
        selected.update(hits)
    return sorted(selected), None


def pytest_argv(files: list[str]) -> list[str]:
    return [PY, "-m", "pytest", "-n", str(min(TEST_WORKERS, 4 * len(files))), *files]


@dataclass
class Plan:
    mode: str
    first: list[tuple[str, list[str]]]
    together: list[tuple[str, list[str]]]
    last: list[tuple[str, list[str]]]
    notes: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def make_plan(full: bool, stamp: dict[str, Any] | None) -> Plan:
    """The steps to run: everything for ``--full`` or when the changed paths cannot be read;
    the docs steps for a Markdown-only change since a pass; else the quick gate."""
    if full:
        return Plan("full", FIRST, TOGETHER, LAST, ["gate: full (--full): every check CI runs"])
    try:
        base, since, from_stamp = change_base(stamp)
        changed = changed_paths(base)
    except GIT_ERRORS as e:
        note = f"gate: full, since the changed paths cannot be read ({str(e)[:160]})"
        return Plan("full", FIRST, TOGETHER, LAST, [note])
    head = f"{len(changed)} changed path(s) {since}"
    if from_stamp and changed and all(is_docs(p) for p in changed):
        tests = docs_tests()
        note = f"gate: docs: {head}, all docs; the docs check and {len(tests)} docs test file(s)"
        return Plan("docs", [], [("pytest", pytest_argv(tests))] if tests else [], LAST, [note])
    named = dict(TOGETHER)
    plan = Plan("quick", FIRST, [], LAST, [f"gate: quick: {head}"])
    files, whole = select_tests(changed)
    if whole is not None:
        plan.notes.append(f"tests: the whole suite ({whole})")
        if "pytest" in named:
            plan.together.append(("pytest", named["pytest"]))
    elif files:
        plan.notes.append(f"tests: {len(files)} file(s) for the changed paths: {', '.join(files)}")
        plan.together.append(("pytest", pytest_argv(files)))
    else:
        plan.notes.append("tests: none map to the changed paths")
        plan.skipped["pytest"] = "skipped (no test maps to the changed paths)"
    if any(p.startswith(CODE_DIRS) for p in changed):
        if "evals" in named:
            plan.together.append(("evals", named["evals"]))
    else:
        plan.notes.append("evals: skipped (no code changed)")
        plan.skipped["evals"] = "skipped (no code changed)"
    return plan


# --- One gate at a time on this machine (W-16) -----------------------------------------------

LOCK_POLL_S = 5.0
UNWRITTEN_LOCK_STALE_S = 60.0
"""A lock file still empty this long after its creation belongs to a gate that died writing it."""


def machine_dir() -> Path:
    """SCIO's per-machine directory, outside every repository: ``%LOCALAPPDATA%/SCIO`` on
    Windows, ``$XDG_CACHE_HOME/scio`` (``~/.cache/scio``) elsewhere. Read at each call, so a test
    can point it at ``tmp_path``."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "SCIO"
    cache = os.environ.get("XDG_CACHE_HOME")
    return (Path(cache) if cache else Path.home() / ".cache") / "scio"


def lock_path() -> Path:
    return machine_dir() / "gate.lock"


def pid_alive(pid: int) -> bool:
    """Whether process ``pid`` runs. Never ``os.kill(pid, 0)`` on Windows: signal 0 is
    CTRL_C_EVENT there, and any other signal terminates the process."""
    if not 0 < pid <= 0xFFFFFFFF:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE; an exited process may linger as an object
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stale(path: Path, holder: dict[str, Any] | None) -> bool:
    if holder is None:  # being written now, unless it has been empty for a minute
        try:
            return time.time() - path.stat().st_mtime > UNWRITTEN_LOCK_STALE_S
        except OSError:
            return False
    pid = holder.get("pid")
    return not (isinstance(pid, int) and pid_alive(pid))


def acquire_lock(path: Path, worktree: str) -> bool:
    """W-16: take the machine-wide gate lock, created atomically (``O_CREAT | O_EXCL``) with
    this gate's pid, worktree and start time. While another gate holds it, say whose and poll
    every ``LOCK_POLL_S`` seconds; a lock whose process is gone is stale and taken over.
    Returns whether it waited."""
    path.parent.mkdir(parents=True, exist_ok=True)
    waited, shown, denied = False, "", 0
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            denied = 0
        except PermissionError:  # Windows: the old lock is being deleted right now
            denied += 1
            if denied > 50:
                raise
            time.sleep(0.1)
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"pid": os.getpid(), "worktree": worktree, "at": _now()}, f)
            return waited
        holder = _read_json(path)
        if _stale(path, holder):
            who = f"pid {holder.get('pid')}" if holder else "never written"
            print(f"gate: taking over a stale lock ({who})", flush=True)
            with contextlib.suppress(OSError):
                if _read_json(path) == holder:
                    path.unlink()
            continue
        if holder is not None:
            line = (
                f"gate: waiting for the gate in {holder.get('worktree')} "
                f"(pid {holder.get('pid')}, since {holder.get('at')})"
            )
            if line != shown:
                print(line, flush=True)
                shown = line
        waited = True
        time.sleep(LOCK_POLL_S)


def release_lock(path: Path) -> None:
    holder = _read_json(path)
    if holder is not None and holder.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink()


# --- The gate ---------------------------------------------------------------------------------


def _execute(plan: Plan, logs: Path, start: float) -> tuple[int, dict[str, str]]:
    counts: dict[str, str] = {}
    for name, argv in plan.first:
        step = _run(name, argv, logs)
        counts[name] = _report(step)
        if step.code != 0:
            return 1, counts
    failed = False
    if plan.together:
        with ThreadPoolExecutor(max_workers=len(plan.together)) as pool:
            together = list(pool.map(lambda s: _run(s[0], s[1], logs), plan.together))
        for step in together:
            counts[step.name] = _report(step)
            failed = failed or step.code != 0
    for name, argv in plan.last:
        step = _run(name, argv, logs)
        counts[name] = _report(step)
        failed = failed or step.code != 0
    counts.update(plan.skipped)
    print(f"gate: {'FAILED' if failed else 'passed'} in {(time.monotonic() - start) / 60:.1f} min")
    return (1 if failed else 0), counts


def _stamp(tree: str | None, mode: str, counts: dict[str, str]) -> None:
    if tree is None:
        print("gate: not a git work tree with a commit; no stamp written")
        return
    try:
        now, head = tested_tree(), _git("rev-parse", "HEAD").strip()
    except GIT_ERRORS as e:
        print(f"gate: cannot read the tree ({e}); no stamp written")
        return
    if now != tree:
        print(
            f"gate: the files changed during the gate (tree {tree[:12]}, now {now[:12]}); "
            "no stamp written, so gate again"
        )
        return
    path = write_stamp(tree, head, mode, counts)
    print(f"gate: stamped tree {tree[:12]} ({mode}) in {path}")
    with contextlib.suppress(*GIT_ERRORS):
        untracked = [
            p for p in _git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if p
        ]
        if untracked:
            shown = ", ".join(untracked[:5]) + (", ..." if len(untracked) > 5 else "")
            print(
                f"gate: the stamped tree holds {len(untracked)} untracked file(s) ({shown}). "
                "Commit them with the rest, or the pushed tree is not this one and the push "
                "gate refuses it; delete or ignore them and gate again instead."
            )


def _gate(full: bool, tree: str | None) -> int:
    plan = make_plan(full, read_stamp())
    logs = ROOT / GATE_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logs.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    print(f"gate: logs in {logs}", flush=True)
    for note in plan.notes:
        print(note, flush=True)
    marker = _mark_running(logs)
    try:
        code, counts = _execute(plan, logs, start)
        if code == 0:
            _stamp(tree, plan.mode, counts)
        return code
    finally:
        _clear_running(marker)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--full",
        action="store_true",
        help="run every check CI runs: a release cut, after a red CI, a change to the gate",
    )
    p.add_argument("--no-lock", action="store_true", help="skip the machine lock (tests)")
    return p


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(list(argv))
    if isinstance(sys.stdout, io.TextIOWrapper):  # a cp1252 console cannot print every log line
        sys.stdout.reconfigure(errors="replace")
    tree = _tree_or_none()
    if _already_passed(tree, args.full):
        return 0
    missing = missing_distributions(ROOT / "pyproject.toml")
    if missing:
        print(f"gate: this environment lacks what CI installs: {', '.join(missing)}")
        print(f"gate: run `{SYNC}` here first (I-205)")
        return 1
    lock = None if args.no_lock else lock_path()
    waited = acquire_lock(lock, str(ROOT)) if lock is not None else False
    try:
        if waited:  # the files may have changed, or passed, while this gate waited
            tree = _tree_or_none()
            if _already_passed(tree, args.full):
                return 0
        return _gate(args.full, tree)
    finally:
        if lock is not None:
            release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
