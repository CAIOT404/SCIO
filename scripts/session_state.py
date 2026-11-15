"""The state-first inspection in one command (issue #122, W-20 and W-28).

    uv run python scripts/session_state.py              # the facts, for the terminal
    uv run python scripts/session_state.py --markdown   # the same, as the handoff's facts block
    uv run python scripts/session_state.py --markdown --write [--detach]
                                                         # into <main>/.scio/session-state.md

It prints what a session used to gather with about ten commands:
- ``origin/main``'s short sha, and the pinned checkouts ``.scio/live-wt`` and
  ``.scio/recovery-wt``: their heads and how far behind main they are;
- every worktree: its branch, ahead and behind ``origin/main``, whether it is gated (its
  ``.scio/gate/last-pass.json`` holds HEAD's tree), a running gate, and its dirty count;
- the open PRs with their merge state, draft flag and check conclusions;
- the open ``live-run`` and ``proposal`` issues;
- the recovery supervisor's runs and open incidents (``python -m recovery status`` in
  ``.scio/recovery-wt``);
- the next ledger id and guardrail (``next_ids.py``);
- the Workflow v2 checklist's count and open items, when ``scripts/workflow_status.py`` is
  in the checkout.

A source that cannot be read (no ``gh``, no network, no recovery-wt) says so on its line; the
rest still prints. It reads refs as they are; ``--fetch`` fetches ``origin`` first. ``--write``
writes the Markdown to the main checkout's ``.scio/session-state.md``, which the ``Stop`` hook
does after every turn (``--detach`` returns at once and writes in the background), so the
memory handoff keeps only decisions and approvals and points there.

The other session tools (``land.py``, ``move_live_wt.py``) share this file's runner, worktree
list, gate-stamp lookup and recovery-status reader. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
STAMP = Path(".scio") / "gate" / "last-pass.json"
RUNNING = Path(".scio") / "gate" / "RUNNING"
FACTS = Path(".scio") / "session-state.md"
LIVE_WT = Path(".scio") / "live-wt"
RECOVERY_WT = Path(".scio") / "recovery-wt"
ISSUE_LABELS = ("live-run", "proposal")
PR_FIELDS = "number,title,headRefName,baseRefName,isDraft,mergeStateStatus,statusCheckRollup"
OPEN_ITEMS_SHOWN = 12


# The runner: every subprocess of the session tools goes through one, so a test fakes gh,
# git push and the long-running scripts while git itself runs for real.


class Runner:
    """Runs, spawns, sleeps and tells the time. Output is decoded as UTF-8 with replacement:
    Windows' default code page cannot decode every PR title (I-075's machine)."""

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        *,
        input: str | None = None,
        timeout: float | None = 120.0,
        stream: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run to the end. ``stream`` lets the output through to the console (a CI watch)
        instead of capturing it. A missing program or a timeout is exit 127, never a raise."""
        try:
            if stream:
                code = subprocess.run(list(argv), cwd=cwd, timeout=timeout).returncode
                return subprocess.CompletedProcess(list(argv), code, "", "")
            return subprocess.run(
                list(argv),
                cwd=cwd,
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))

    def spawn(
        self, argv: Sequence[str], cwd: Path, log: Path, env: dict[str, str] | None = None
    ) -> int:
        """Start in the background, in its own process group, with every output in ``log``;
        return the pid. Nothing of the caller's pipes is inherited, so a hook that spawns
        returns at once. On Windows the child gets a hidden console (``CREATE_NO_WINDOW``),
        never none (``DETACHED_PROCESS``): a child without a console gives every console
        program it starts (git, gh, pytest's workers) a visible window of its own."""
        log.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        with log.open("ab") as out:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **kwargs,
            )
        return proc.pid

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


def console() -> None:
    """Make stdout print every PR title and the checklist's ``·``. A terminal keeps its
    encoding and replaces what it cannot show; a pipe (a Claude session's Bash tool, a hook)
    gets UTF-8, which is what it decodes, instead of Windows' cp1252."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sibling(name: str, directory: Path = SCRIPTS) -> ModuleType:
    """A script beside this one (or in ``directory``), loaded by its path. These scripts run
    as ``python scripts/x.py`` from any checkout and are loaded by path in tests, so a plain
    import would depend on ``sys.path``. Registered in ``sys.modules`` for ``@dataclass``
    (I-174), under a name unique to its path."""
    path = (directory / f"{name}.py").resolve()
    key = f"_session_tools_{name}_{abs(hash(str(path)))}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[key]
        raise
    return mod


# Worktrees, the main checkout, and gate stamps.


@dataclass
class Worktree:
    path: Path
    head: str
    branch: str | None
    """``feat/x``; ``None`` when detached."""


def worktrees(root: Path, runner: Runner) -> list[Worktree]:
    """Every worktree of the repository at ``root``, the main checkout first. Prunable entries
    and directories that no longer exist are left out."""
    proc = runner.run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        return []
    found: list[Worktree] = []
    for block in proc.stdout.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields or "bare" in fields or "prunable" in fields:
            continue
        path = Path(fields["worktree"])
        if not path.is_dir():
            continue
        branch = fields.get("branch", "").removeprefix("refs/heads/") or None
        found.append(Worktree(path, fields.get("HEAD", ""), branch))
    return found


def main_checkout(root: Path, runner: Runner) -> Path:
    """The repository's main checkout: the first entry of ``git worktree list``. The shared
    files (``.scio/approvals.md``, ``queue``, ``agents/``, ``live-wt``, ``recovery-wt``,
    ``session-state.md``) live there, whichever worktree a tool runs from."""
    proc = runner.run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :])
    return root


def read_stamp(worktree: Path) -> dict[str, Any] | None:
    """The gate's last pass in ``worktree`` (``{"tree", "head", "mode", "at", "counts"}``),
    or ``None`` when there is none or it cannot be read."""
    try:
        data = json.loads((worktree / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("tree") else None


def tree_of(root: Path, runner: Runner, rev: str) -> str | None:
    """The tree id of commit ``rev``; ``None`` when it is not in this clone."""
    proc = runner.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{rev}^{{tree}}"])
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def find_stamp(root: Path, runner: Runner, tree: str) -> tuple[Path, dict[str, Any]] | None:
    """A worktree whose gate stamp holds ``tree``, with the stamp: the proof that this exact
    content passed the gate somewhere on this machine."""
    for wt in worktrees(root, runner):
        stamp = read_stamp(wt.path)
        if stamp and stamp.get("tree") == tree:
            return wt.path, stamp
    return None


def write_facts(main: Path, text: str) -> Path:
    """Write the facts block to ``<main>/.scio/session-state.md``, whole or not at all."""
    path = main / FACTS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)
    return path


# The recovery supervisor's status.


@dataclass
class RecoveryActivity:
    read: bool
    """Whether ``python -m recovery status`` answered in the expected shape. When it did not,
    the tools treat the supervisor as busy, never as idle."""
    running: list[str] = field(default_factory=list)
    """Lines of runs whose status is ``running``."""
    incidents: list[str] = field(default_factory=list)
    """Lines of open incidents: ``<id>  <state>  <class>  issue #N  PR #M``."""
    error: str = ""


def parse_recovery_status(text: str) -> RecoveryActivity:
    """Read ``python -m recovery status``'s output (``recovery/cli.py``, ``_print_status``):
    a ``runs`` section, then ``open incidents``, then the escalated ones."""
    section = ""
    seen: set[str] = set()
    running: list[str] = []
    incidents: list[str] = []
    for line in text.splitlines():
        if line and not line.startswith(" "):
            if line.startswith("runs"):
                section = "runs"
            elif line.startswith("open incidents"):
                section = "incidents"
            else:
                section = "other"
            seen.add(section)
            continue
        if not line.startswith("  ") or line.startswith("    "):
            continue  # a detail line under an entry
        words = line.split()
        if len(words) < 2 or words[0] == "...":
            continue
        if section == "runs" and words[1] == "running":
            running.append(line.strip())
        elif section == "incidents":
            incidents.append(line.strip())
    read = {"runs", "incidents"} <= seen
    return RecoveryActivity(read, running, incidents, "" if read else "unexpected output")


def recovery_status(main: Path, runner: Runner) -> RecoveryActivity:
    """``python -m recovery status``, run from ``.scio/recovery-wt`` (the supervisor's own
    pinned code). It is read only; nothing here moves or writes recovery-wt."""
    wt = main / RECOVERY_WT
    if not wt.is_dir():
        return RecoveryActivity(False, error=f"no {RECOVERY_WT.as_posix()} in {main}")
    proc = runner.run([sys.executable, "-m", "recovery", "status"], cwd=wt, timeout=60)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).strip().splitlines()
        return RecoveryActivity(False, error=f"exit {proc.returncode}: {why[-1] if why else ''}")
    return parse_recovery_status(proc.stdout)


# Collecting the state.


@dataclass
class WorktreeRow:
    name: str
    branch: str
    head: str
    ahead: int | None
    behind: int | None
    gate: str
    running: bool
    dirty: int | None


@dataclass
class State:
    at: str
    root: Path
    main: Path
    origin_main: str
    base_note: str = ""
    """Says when there is no ``origin/main`` and local ``main`` stood in for it."""
    pinned: dict[str, str] = field(default_factory=dict)
    """``live-wt`` / ``recovery-wt`` -> "fe83a50, behind main by 14"."""
    rows: list[WorktreeRow] = field(default_factory=list)
    prs: list[dict[str, Any]] | str = field(default_factory=list)
    """The open PRs, or why they could not be read."""
    issues: dict[str, list[dict[str, Any]] | str] = field(default_factory=dict)
    recovery: RecoveryActivity | None = None
    ids: tuple[str, int] | str = ""
    workflow: tuple[str, list[str]] | None = None
    """("12/45 closed", ["W-11 · title", ...]); ``None`` without the checklist script."""


def _git(root: Path, runner: Runner, *args: str) -> str:
    proc = runner.run(["git", "-C", str(root), *args])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _base_ref(root: Path, runner: Runner) -> str:
    return (
        "origin/main"
        if _git(root, runner, "rev-parse", "--verify", "-q", "origin/main")
        else "main"
    )


def _ahead_behind(
    root: Path, runner: Runner, base: str, head: str
) -> tuple[int | None, int | None]:
    out = _git(root, runner, "rev-list", "--left-right", "--count", f"{base}...{head}")
    parts = out.split()
    if len(parts) != 2:
        return None, None
    return int(parts[1]), int(parts[0])


def _display(path: Path, main: Path) -> str:
    try:
        rel = path.resolve().relative_to(main.resolve())
    except ValueError:
        return path.as_posix()
    return rel.as_posix() if str(rel) != "." else main.name


def _row(root: Path, main: Path, runner: Runner, base: str, wt: Worktree) -> WorktreeRow:
    ahead, behind = _ahead_behind(root, runner, base, wt.head) if wt.head else (None, None)
    stamp = read_stamp(wt.path)
    tree = tree_of(root, runner, wt.head) if wt.head else None
    if stamp is None:
        gate = "no stamp"
    elif tree and stamp.get("tree") == tree:
        gate = f"gated ({stamp.get('mode', '?')})"
    else:
        gate = "stamp stale"
    status = runner.run(["git", "-C", str(wt.path), "status", "--porcelain"], timeout=120)
    dirty = len(status.stdout.splitlines()) if status.returncode == 0 else None
    return WorktreeRow(
        _display(wt.path, main),
        wt.branch or "(detached)",
        wt.head[:7],
        ahead,
        behind,
        gate,
        (wt.path / RUNNING).exists(),
        dirty,
    )


def _gh_json(runner: Runner, root: Path, args: list[str]) -> list[dict[str, Any]] | str:
    proc = runner.run(["gh", *args], cwd=root, timeout=60)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).strip().splitlines()
        return f"unavailable (gh exit {proc.returncode}: {why[-1] if why else 'no output'})"
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return "unavailable (gh printed no JSON)"
    return data if isinstance(data, list) else "unavailable (unexpected JSON)"


def _workflow(root: Path) -> tuple[str, list[str]] | None:
    script = root / "scripts" / "workflow_status.py"
    if not script.is_file():
        return None
    try:
        ws = sibling("workflow_status", script.parent)
        rows, errors = ws.report(root)
    except Exception as exc:  # the checklist is a guest here: its failure is one line
        return (f"unreadable ({type(exc).__name__}: {exc})", [])
    closed_states = getattr(ws, "CLOSED", ("done", "superseded", "dropped"))
    closed = sum(1 for _, s in rows if s in closed_states)
    count = f"{closed}/{len(rows)} closed" + (f", {len(errors)} problem(s)" if errors else "")
    return count, [f"{it.id} · {it.title}" for it, s in rows if s not in closed_states]


def collect(root: Path, runner: Runner, *, fetch: bool = False) -> State:
    """Gather every fact; independent sources are read side by side."""
    if fetch:
        runner.run(["git", "-C", str(root), "fetch", "--quiet", "origin"], timeout=120)
    main = main_checkout(root, runner)
    base = _base_ref(root, runner)
    state = State(
        at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        root=root,
        main=main,
        origin_main=_git(root, runner, "rev-parse", "--short", base) or "?",
        base_note="" if base == "origin/main" else " (local main: no origin/main here)",
    )
    wts = worktrees(root, runner)
    next_ids = sibling("next_ids")
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [pool.submit(_row, root, main, runner, base, wt) for wt in wts]
        prs = pool.submit(
            _gh_json, runner, root, ["pr", "list", "--state", "open", "--json", PR_FIELDS]
        )
        issues = {
            label: pool.submit(
                _gh_json,
                runner,
                root,
                ["issue", "list", "--state", "open", "--label", label, "--json", "number,title"],
            )
            for label in ISSUE_LABELS
        }
        recovery = pool.submit(recovery_status, main, runner)
        ids = pool.submit(next_ids.next_ids, root, runner)
        workflow = pool.submit(_workflow, root)
        state.rows = [f.result() for f in rows]
        state.prs = prs.result()
        state.issues = {label: f.result() for label, f in issues.items()}
        state.recovery = recovery.result()
        try:
            state.ids = ids.result()
        except Exception as exc:
            state.ids = f"unavailable ({type(exc).__name__}: {exc})"
        state.workflow = workflow.result()
    for name in (LIVE_WT, RECOVERY_WT):
        path = main / name
        row = next((r for r in state.rows if r.name == name.as_posix()), None)
        if row is None:
            state.pinned[name.name] = "missing" if not path.exists() else "not a worktree"
        else:
            behind = "?" if row.behind is None else str(row.behind)
            state.pinned[name.name] = f"{row.head}, behind main by {behind}"
    return state


# Rendering.


def checks_summary(rollup: list[dict[str, Any]] | None) -> str:
    """``4 success`` or ``1 failure, 3 success`` or ``2 pending``, from statusCheckRollup."""
    counts: dict[str, int] = {}
    for check in rollup or []:
        if check.get("__typename") == "StatusContext":
            verdict = str(check.get("state") or "pending")
        elif str(check.get("status", "")).upper() not in ("", "COMPLETED"):
            verdict = "pending"
        else:
            verdict = str(check.get("conclusion") or "pending")
        verdict = verdict.lower()
        counts[verdict] = counts.get(verdict, 0) + 1
    if not counts:
        return "no checks"
    return ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))


def _count(n: int | None) -> str:
    return "?" if n is None else str(n)


def _ids(state: State) -> str:
    if isinstance(state.ids, str):
        return state.ids or "?"
    ledger, guard = state.ids
    return f"{ledger}, guardrail {guard}"


def _recovery_lines(rec: RecoveryActivity | None) -> list[str]:
    if rec is None:
        return ["not read"]
    if not rec.read:
        return [f"unavailable: {rec.error}"]
    lines = [f"running supervised runs: {len(rec.running) or 'none'}"]
    lines += [f"  {r}" for r in rec.running]
    lines.append(f"open incidents: {len(rec.incidents) or 'none'}")
    lines += [f"  {i}" for i in rec.incidents]
    return lines


def render_text(state: State) -> str:
    out = [f"SCIO session state, {state.at} ({state.main.as_posix()})"]
    out.append(f"origin/main   {state.origin_main}{state.base_note}")
    pinned = "; ".join(f"{k} {v}" for k, v in state.pinned.items())
    out.append(f"pinned        {pinned}")
    out.append(f"next ids      {_ids(state)}")
    out.append("worktrees     (branch, ahead/behind origin/main, gate, dirty files)")
    width = max((len(r.name) for r in state.rows), default=10)
    bwidth = max((len(r.branch) for r in state.rows), default=10)
    for r in state.rows:
        running = "  GATE RUNNING" if r.running else ""
        moved = f"+{_count(r.ahead)}/-{_count(r.behind)}"
        out.append(
            f"  {r.name:<{width}}  {r.branch:<{bwidth}}  {moved:<9}  {r.gate:<14}"
            f"  dirty {_count(r.dirty)}{running}"
        )
    if isinstance(state.prs, str):
        out.append(f"open PRs      {state.prs}")
    else:
        out.append(f"open PRs      {len(state.prs) or 'none'}")
        for pr in state.prs:
            draft = "draft" if pr.get("isDraft") else "ready"
            out.append(
                f"  #{pr.get('number')}  {pr.get('headRefName')} -> {pr.get('baseRefName')}  "
                f"{pr.get('mergeStateStatus')}  {draft}  checks: "
                f"{checks_summary(pr.get('statusCheckRollup'))}  {pr.get('title')}"
            )
    for label, issues in state.issues.items():
        if isinstance(issues, str):
            out.append(f"{label:<13} {issues}")
        else:
            out.append(f"{label:<13} {len(issues) or 'none'} open")
            out += [f"  #{i.get('number')}  {i.get('title')}" for i in issues]
    out.append("recovery      (python -m recovery status in .scio/recovery-wt)")
    out += [f"  {line}" for line in _recovery_lines(state.recovery)]
    if state.workflow is None:
        out.append("workflow v2   no scripts/workflow_status.py in this checkout")
    else:
        count, open_items = state.workflow
        out.append(f"workflow v2   {count}")
        out += [f"  open: {item}" for item in open_items[:OPEN_ITEMS_SHOWN]]
        if len(open_items) > OPEN_ITEMS_SHOWN:
            out.append(f"  ... and {len(open_items) - OPEN_ITEMS_SHOWN} more")
    return "\n".join(out)


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(state: State) -> str:
    """The handoff's facts block (W-28): everything a new session must not take from memory."""
    out = [
        "# Session state",
        "",
        f"Generated by `scripts/session_state.py --markdown` at {state.at}. Facts only, read "
        "from git, GitHub and the recovery supervisor; decisions and approvals stay in the "
        "memory handoff and `.scio/approvals.md`.",
        "",
        f"- **origin/main:** `{state.origin_main}`{state.base_note}",
    ]
    for name, value in state.pinned.items():
        note = " (moves only on the owner's word)" if name == RECOVERY_WT.name else ""
        out.append(f"- **{name}:** {value}{note}")
    out.append(f"- **Next ids:** {_ids(state)}")
    out += [
        "",
        "## Worktrees",
        "",
        "| Worktree | Branch | Head | Ahead | Behind | Gate | Dirty |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in state.rows:
        gate = r.gate + (", RUNNING" if r.running else "")
        out.append(
            f"| {_cell(r.name)} | {_cell(r.branch)} | `{r.head}` | {_count(r.ahead)} | "
            f"{_count(r.behind)} | {gate} | {_count(r.dirty)} |"
        )
    out += ["", "## Open PRs", ""]
    if isinstance(state.prs, str):
        out.append(state.prs)
    elif not state.prs:
        out.append("None.")
    else:
        out += [
            "| PR | Branch | Base | Merge state | Draft | Checks |",
            "|---|---|---|---|---|---|",
        ]
        for pr in state.prs:
            out.append(
                f"| #{pr.get('number')} {_cell(pr.get('title'))} | {_cell(pr.get('headRefName'))}"
                f" | {_cell(pr.get('baseRefName'))} | {pr.get('mergeStateStatus')} | "
                f"{'yes' if pr.get('isDraft') else 'no'} | "
                f"{checks_summary(pr.get('statusCheckRollup'))} |"
            )
    out += ["", "## Open issues", ""]
    for label, issues in state.issues.items():
        if isinstance(issues, str):
            out.append(f"- `{label}`: {issues}")
        elif not issues:
            out.append(f"- `{label}`: none")
        else:
            out += [f"- `{label}` #{i.get('number')}: {i.get('title')}" for i in issues]
    out += ["", "## Recovery", ""]
    out += [f"- {line.strip()}" for line in _recovery_lines(state.recovery)]
    out += ["", "## Workflow v2", ""]
    if state.workflow is None:
        out.append("No `scripts/workflow_status.py` in this checkout.")
    else:
        count, open_items = state.workflow
        out.append(f"{count} (`docs/WORKFLOW_V2.md`).")
        if open_items:
            out += ["", "Open:", ""]
            out += [f"- {item}" for item in open_items]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    ap.add_argument("--markdown", action="store_true", help="the handoff's facts block")
    ap.add_argument(
        "--write", action="store_true", help="write it to <main>/.scio/session-state.md"
    )
    ap.add_argument(
        "--detach", action="store_true", help="with --write: return at once, write in background"
    )
    ap.add_argument("--fetch", action="store_true", help="fetch origin first")
    args = ap.parse_args(argv)
    runner = runner or Runner()
    if args.detach:
        main_co = main_checkout(args.root, runner)
        child = [sys.executable, str(Path(__file__).resolve()), "--root", str(args.root)]
        child += ["--markdown", "--write"] + (["--fetch"] if args.fetch else [])
        runner.spawn(child, args.root, main_co / ".scio" / "session-state.log")
        return 0
    state = collect(args.root, runner, fetch=args.fetch)
    text = render_markdown(state) if args.markdown or args.write else render_text(state)
    if args.write:
        try:
            write_facts(state.main, text)
        except OSError as exc:  # a Stop hook never fails the turn
            print(f"session-state: could not write {state.main / FACTS}: {exc}", file=sys.stderr)
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
