"""``python -m recovery``: run, wait, tick, status, incidents, hold, release, dismiss, watch.

``run`` exits with the child's own exit code, so a session that called ``sh scio.sh
build ...`` before sees the same status and output under the supervisor. Every other
error prints one line and returns 2, never a traceback.

``run --detach`` starts that same supervised run as a detached process and returns at
once, and ``wait`` blocks until the workspace's run ends and exits with its code (issue
#175). A build started attached dies with the session that started it; a detached one
belongs to no session, and ``wait`` only reads, so it is safe to kill.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from recovery import config, history, machine, procs, runner, watch
from recovery.config import WorkspaceConfig
from recovery.model import Incident, IncidentState, ProcessKey, RunRecord, RunStatus
from recovery.store import Store

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
"""The directory that holds ``recovery/``: a background tick starts from here."""

RUN_ID = re.compile(r"^run-\d{14}-[0-9a-f]{6}$")
"""``runner.new_run_id``'s shape. ``--run-id`` names a directory, so nothing else passes."""

DETACH_WAIT_S = 10.0
"""How long ``run --detach`` waits for the detached supervisor's run record."""
DETACH_POLL_S = 0.1

WAIT_SETTLE_S = 60.0
"""How long ``wait`` gives the supervisor of an ended run to record its incident."""
WAIT_SETTLE_POLL_S = 1.0

STATUS_RUNS = 20
"""How many runs ``status`` lists."""
STATUS_ESCALATED = 10
STATUS_DISMISSED = 5
"""How many dismissed incidents ``status`` lists."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m recovery",
        description="SCIO's external recovery supervisor (ADR 0019).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="run the workspace's SCIO launcher as a supervised child",
        description=(
            "Runs <launcher> ARGS in the workspace, echoes its output, and exits with its "
            "code. A failure opens or joins an incident, and a detached tick carries the "
            "recovery on."
        ),
    )
    run.add_argument("--workspace", required=True, help="directory holding recovery.toml")
    run.add_argument("--incident", help="the incident this run resumes (set by a replay)")
    run.add_argument("--resumed-from", help="the run id this run replays (set by a replay)")
    run.add_argument(
        "--detach",
        action="store_true",
        help=(
            "start the same supervised run as a detached process that no session owns, "
            "print its run id and stdout path, and return at once (then: wait)"
        ),
    )
    run.add_argument("--run-id", help=argparse.SUPPRESS)  # set by run --detach
    run.add_argument("args", nargs=argparse.REMAINDER, help="-- then the SCIO arguments")

    wait = sub.add_parser(
        "wait",
        help="block until the workspace's supervised run ends; exit with its code",
        description=(
            "Waits for the workspace's newest supervised run (or the one --run names) to "
            "end, prints the tail of its stdout and exits with its exit code: 2 for a run "
            "that was refused, 1 for one with no exit code (killed, orphaned). When the run "
            "has already ended it reports it at once. It takes no lease and writes nothing, "
            "so killing it never affects the run."
        ),
    )
    wait.add_argument("--workspace", required=True, help="directory holding recovery.toml")
    wait.add_argument("--run", help="the run id to wait for (default: the newest run)")
    wait.add_argument("--tail", type=int, default=60, help="stdout lines to print at the end")
    wait.add_argument("--interval", type=float, default=5.0, help="seconds between polls")

    tick = sub.add_parser("tick", help="reconcile orphans and advance every open incident")
    tick.add_argument("--incident", help="advance only this incident")

    status = sub.add_parser("status", help="runs and incidents, with their log paths")
    status.add_argument("--workspace")

    inc = sub.add_parser("incidents", help="search incident history")
    inc.add_argument("--signature")
    inc.add_argument("--grep")
    inc.add_argument("--limit", type=int, default=20)

    hold = sub.add_parser("hold", help="pause an incident's automation")
    hold.add_argument("incident")
    rel = sub.add_parser("release", help="resume a held incident")
    rel.add_argument("incident")
    dis = sub.add_parser(
        "dismiss",
        help="end an incident that was not a SCIO fault: no repair, no replay",
        description=(
            "Ends an open incident (a held one too) as dismissed, with the reason on its "
            "timeline and on its issue. It starts no repair and no replay and leaves live-wt "
            "as it is. Exits 1, changing nothing, for an incident that is already closed, "
            "escalated or dismissed, and while a repair session of it is still running."
        ),
    )
    dis.add_argument("incident")
    dis.add_argument("--reason", required=True, help="why it was not a SCIO fault")

    watch_cmd = sub.add_parser(
        "watch",
        help="an incident's state, timeline and repair session, live",
        description=(
            "Prints the incident's header and timeline, and the last steps of the Claude "
            "Code repair session working in its worktree. With --follow, it then prints only "
            "what is new until the incident is closed or escalated (Ctrl+C stops it)."
        ),
    )
    watch_cmd.add_argument("incident")
    watch_cmd.add_argument("--follow", action="store_true", help="keep printing what is new")
    watch_cmd.add_argument("--last", type=int, default=20, help="session steps to show first")
    watch_cmd.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    return p


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _home() -> Path:
    return config.recovery_home()


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _when(stamp: float | None) -> str:
    if not stamp:
        return "-"
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# run


def _start_tick(incident: Incident, home: Path) -> int:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    log = home / "ticks" / f"{incident.incident_id}-{stamp}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env[config.HOME_ENV] = str(home)
    return procs.spawn_detached(
        [sys.executable, "-m", "recovery", "tick", "--incident", incident.incident_id],
        cwd=PACKAGE_ROOT,
        env=env,
        log_path=log,
    )


def _run(args: argparse.Namespace) -> int:
    scio_args = list(args.args)
    if scio_args and scio_args[0] == "--":
        scio_args = scio_args[1:]
    if not scio_args:
        _err("recovery run: give the SCIO arguments after --, e.g. -- build --product P")
        return 2
    workspace = Path(args.workspace)
    try:
        cfg = WorkspaceConfig.load(workspace)
    except FileNotFoundError:
        _err(f"recovery run: no recovery.toml in {workspace.resolve()}")
        return 2
    except ValueError as exc:  # tomllib.TOMLDecodeError is a ValueError
        _err(f"recovery run: {workspace.resolve() / 'recovery.toml'}: {exc}")
        return 2
    if args.run_id and not RUN_ID.match(args.run_id):
        _err(f"recovery run: {args.run_id!r} is not a run id")
        return 2
    home = _home()
    if args.detach:
        return _run_detached(args, cfg, scio_args, home)
    # Only a detached start names the run; an attached run's call is as it was.
    supervise = runner.supervise
    if args.run_id:
        supervise = functools.partial(runner.supervise, run_id=args.run_id)
    store = Store.at_home(home)
    try:
        record = supervise(
            cfg,
            store,
            scio_args,
            env=os.environ,
            home=home,
            incident_id=args.incident,
            resumed_from=args.resumed_from,
        )
        holder = store.lease_holder(cfg.lease_name)
    finally:
        store.close()
    if record.status is RunStatus.REFUSED:
        _err(_busy_line(cfg, holder))
        return 2
    code = 1 if record.exit_code is None else record.exit_code
    # From here on, a supervisor problem never changes the child's exit code.
    try:
        deps = machine.default_deps(home, PACKAGE_ROOT)
        incident = machine.on_run_finished(record, deps)
    except Exception as exc:
        _err(f"recovery: run {record.run_id} ended, but it was not recorded: {exc!r}")
        return code
    if incident is not None and incident.open:
        try:
            _start_tick(incident, home)
        except Exception as exc:
            _err(
                f"recovery: incident {incident.incident_id}: the background tick did not "
                f"start ({exc!r}); run python -m recovery tick"
            )
        else:
            _err(_incident_opened_line(incident))
    return code


def _busy_line(cfg: WorkspaceConfig, holder: ProcessKey | None) -> str:
    who = f"pid {holder.pid}" if holder is not None else "another process"
    return f"recovery run: {cfg.workspace} is busy: {who} holds its lease; nothing started"


def _incident_opened_line(incident: Incident) -> str:
    return (
        f"recovery: incident {incident.incident_id} ({incident.failure_class.value}) "
        "opened; recovery continues in the background. python -m recovery status"
    )


def _run_detached(
    args: argparse.Namespace, cfg: WorkspaceConfig, scio_args: list[str], home: Path
) -> int:
    """``run --detach``: refuse a busy workspace as an attached run does, then start the
    ordinary supervised run detached and name it (issue #175).

    This process takes no lease and saves no record: the detached supervisor does both.
    It makes the run id, so it can print it, and waits at most :data:`DETACH_WAIT_S` for
    the supervisor's record, so that a ``wait`` started next finds this run and not the
    one before it."""
    store = Store.at_home(home)
    try:
        holder = store.lease_holder(cfg.lease_name)
        if holder is not None and procs.alive(holder):
            _err(_busy_line(cfg, holder))
            return 2
        run_id = args.run_id or runner.new_run_id()
        run_dir = home / "runs" / run_id
        try:
            pid = runner.start_detached(
                cfg.workspace,
                scio_args,
                run_id=run_id,
                env=os.environ,
                home=home,
                package_root=PACKAGE_ROOT,
                incident_id=args.incident,
                resumed_from=args.resumed_from,
                spawn_detached=procs.spawn_detached,
            )
        except OSError as exc:
            _err(f"recovery run: the detached run did not start: {exc}")
            return 1
        deadline = time.monotonic() + DETACH_WAIT_S
        record = store.get_run(run_id)
        while record is None and time.monotonic() < deadline:
            time.sleep(DETACH_POLL_S)
            record = store.get_run(run_id)
        holder = store.lease_holder(cfg.lease_name)
    finally:
        store.close()
    if record is None:
        _err(
            f"recovery run: detached run {run_id} (pid {pid}) recorded nothing in "
            f"{DETACH_WAIT_S:g} s; its log directory is {run_dir}"
        )
        return 1
    if record.status is RunStatus.REFUSED:  # another run took the lease in between
        _err(_busy_line(cfg, holder))
        return 2
    print(
        f"recovery run: detached run {run_id} (pid {record.supervisor_pid or pid}); "
        f"stdout: {record.stdout_path}",
        flush=True,
    )
    return 0


# wait


def _pick_run(store: Store, workspace: str, run_id: str | None) -> RunRecord | str:
    """The run ``wait`` reports, or the one line that says why there is none.

    Without ``--run`` it is the workspace's newest run that was started. A refused attempt
    (a second ``run`` on a busy workspace) is never picked over the run that refused it;
    it is reported only when the workspace has nothing else."""
    if run_id is not None:
        run = store.get_run(run_id)
        if run is None:
            return f"no run {run_id} (python -m recovery status)"
        if not _same_dir(run.workspace, workspace):
            return f"run {run_id} is a run of {run.workspace}, not of {Path(workspace).resolve()}"
        return run
    runs = [r for r in store.runs() if _same_dir(r.workspace, workspace)]
    if not runs:
        return f"no supervised run of {Path(workspace).resolve()} is recorded"
    started = [r for r in runs if r.status is not RunStatus.REFUSED]
    return (started or runs)[-1]


def _await_end(store: Store, run_id: str, interval_s: float) -> tuple[RunRecord, bool]:
    """Poll the store until the run has ended. Returns its record, and whether it is an
    orphan nobody recorded yet: still ``running`` in the store, with its supervisor and
    its build both gone (what the next tick marks ``orphaned``).

    A supervisor saves the run's end, and then records the incident. So an ended run is
    reported once its supervisor is gone too, or :data:`WAIT_SETTLE_S` later."""
    settle_until: float | None = None
    while True:
        run = store.get_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id} is no longer in the store")
        supervisor = ProcessKey(run.supervisor_pid, run.supervisor_created)
        if run.status is not RunStatus.RUNNING:
            if settle_until is None:
                settle_until = time.monotonic() + WAIT_SETTLE_S
            if not procs.alive(supervisor) or time.monotonic() >= settle_until:
                return run, False
            time.sleep(min(interval_s, WAIT_SETTLE_POLL_S))
            continue
        if not procs.alive(supervisor) and not procs.alive(run.process):
            return run, True
        time.sleep(interval_s)


def _tail_offset(data: bytes, lines: int) -> int:
    """Where the last ``lines`` lines of ``data`` start."""
    if lines <= 0:
        return len(data)
    pos = len(data.rstrip(b"\r\n"))
    for _ in range(lines):
        pos = data.rfind(b"\n", 0, pos)
        if pos < 0:
            return 0
    return pos + 1


def _print_tail(path: str, lines: int) -> None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return
    procs.follow([(Path(path), sys.stdout)], [_tail_offset(data, lines)])


def _wait(args: argparse.Namespace) -> int:
    home = _home()
    if not (home / "state.sqlite").is_file():  # never create a store to wait on it
        _err(f"recovery wait: no supervisor store in {home}")
        return 2
    store = Store.at_home(home)
    try:
        picked = _pick_run(store, args.workspace, args.run)
        if isinstance(picked, str):
            _err(f"recovery wait: {picked}")
            return 2
        try:
            run, orphan = _await_end(store, picked.run_id, args.interval)
        except KeyboardInterrupt:
            _err(f"recovery wait: stopped waiting; run {picked.run_id} is not affected")
            return 130
        incident = store.get_incident(run.incident_id) if run.incident_id else None
    finally:
        store.close()
    if run.stdout_path:
        _print_tail(run.stdout_path, args.tail)
    sys.stdout.flush()
    if orphan:
        _err(
            f"recovery wait: run {run.run_id} has no exit: its supervisor and its build "
            "are gone. python -m recovery tick records the orphan"
        )
        return 1
    code = "-" if run.exit_code is None else str(run.exit_code)
    _err(f"recovery wait: run {run.run_id} {run.status.value}, exit {code}")
    if incident is not None and incident.open:
        _err(_incident_opened_line(incident))
    if run.status is RunStatus.REFUSED:
        return 2
    return 1 if run.exit_code is None else run.exit_code


# tick, hold, release


def _tick(args: argparse.Namespace) -> int:
    deps = machine.default_deps(_home(), PACKAGE_ROOT)
    if args.incident:
        incident = machine.drive(args.incident, deps)
        print(f"{incident.incident_id}: {incident.state.value}")
        if incident.escalation:
            print(f"  escalation: {incident.escalation}")
        return 0
    lines = machine.tick(deps)
    for line in lines:
        print(line)
    if not lines:
        print("nothing to do")
    return 0


def _hold(args: argparse.Namespace) -> int:
    incident = machine.hold(args.incident, machine.default_deps(_home(), PACKAGE_ROOT))
    held_from = incident.held_from.value if incident.held_from else "-"
    print(f"{incident.incident_id}: held (from {held_from})")
    return 0


def _release(args: argparse.Namespace) -> int:
    incident = machine.release(args.incident, machine.default_deps(_home(), PACKAGE_ROOT))
    print(f"{incident.incident_id}: released to {incident.state.value}")
    return 0


def _dismiss(args: argparse.Namespace) -> int:
    deps = machine.default_deps(_home(), PACKAGE_ROOT)
    try:
        incident = machine.dismiss(args.incident, args.reason, deps)
    except machine.DismissRefused as exc:
        _err(f"recovery dismiss: {exc}")
        return 1
    print(f"{incident.incident_id}: dismissed ({incident.dismissal})")
    return 0


# status, incidents


def _run_lines(run: RunRecord) -> list[str]:
    code = "-" if run.exit_code is None else str(run.exit_code)
    lines = [
        f"  {run.run_id}  {run.status.value}  exit {code}  {' '.join(run.argv)}",
        f"    started {_when(run.started_at)}, ended {_when(run.ended_at)}",
    ]
    if run.downtime_s:
        lines.append(f"    downtime: {run.downtime_s / 3600:.1f} h (not counted as idle)")
    if run.incident_id:
        lines.append(f"    incident: {run.incident_id}")
    if run.resumed_from:
        lines.append(f"    replays: {run.resumed_from}")
    lines.append(f"    dir: {run.run_dir or '-'}")
    if run.stdout_path:
        lines.append(f"    stdout: {run.stdout_path}")
    if run.stderr_path:
        lines.append(f"    stderr: {run.stderr_path}")
    return lines


def _incident_line(incident: Incident) -> list[str]:
    issue = f"#{incident.issue_number}" if incident.issue_number is not None else "-"
    pr = f"#{incident.pr_number}" if incident.pr_number is not None else "-"
    lines = [
        f"  {incident.incident_id}  {incident.state.value}  {incident.failure_class.value}  "
        f"issue {issue}  PR {pr}",
        f"    {incident.summary}",
    ]
    if incident.escalation:
        lines.append(f"    escalation: {incident.escalation}")
    if incident.dismissal:
        lines.append(f"    dismissed: {incident.dismissal}")
    return lines


def _status(args: argparse.Namespace) -> int:
    store = Store.at_home(_home())
    try:
        _print_status(store, args.workspace)
    finally:
        store.close()
    return 0


def _print_status(store: Store, workspace_filter: str | None) -> None:
    def wanted(workspace: str) -> bool:
        return workspace_filter is None or _same_dir(workspace, workspace_filter)

    runs = [r for r in reversed(store.runs()) if wanted(r.workspace)]
    print(f"runs ({len(runs)}, newest first):" if runs else "runs: none")
    for run in runs[:STATUS_RUNS]:
        for line in _run_lines(run):
            print(line)
    if len(runs) > STATUS_RUNS:
        print(f"  ... {len(runs) - STATUS_RUNS} older runs")
    open_ones = [i for i in reversed(store.open_incidents()) if wanted(i.workspace)]
    print("open incidents (newest first):" if open_ones else "open incidents: none")
    for incident in open_ones:
        for line in _incident_line(incident):
            print(line)
    escalated = [
        i
        for i in reversed(store.incidents(states=[IncidentState.ESCALATED]))
        if wanted(i.workspace)
    ]
    if escalated:
        print("escalated, waiting for a human (newest first):")
        for incident in escalated[:STATUS_ESCALATED]:
            for line in _incident_line(incident):
                print(line)
    # Its own section, after the open ones: a reader of this text (scripts/session_state.py)
    # counts only the entries under "open incidents".
    dismissed = [
        i
        for i in reversed(store.incidents(states=[IncidentState.DISMISSED]))
        if wanted(i.workspace)
    ]
    if dismissed:
        print("dismissed by the owner, not open (newest first):")
        for incident in dismissed[:STATUS_DISMISSED]:
            for line in _incident_line(incident):
                print(line)
        if len(dismissed) > STATUS_DISMISSED:
            print(f"  ... {len(dismissed) - STATUS_DISMISSED} older dismissed incidents")


def _incidents(args: argparse.Namespace) -> int:
    store = Store.at_home(_home())
    try:
        found = history.search(store, signature=args.signature, text=args.grep, limit=args.limit)
    finally:
        store.close()
    if not found:
        print("no incidents")
        return 0
    for incident in found:
        print(
            f"{incident.incident_id}  {_when(incident.created_at)}  {incident.state.value}  "
            f"{incident.failure_class.value}  {incident.signature}"
        )
        print(f"  {incident.summary}")
        facts = [
            f"frame {incident.innermost_scio_frame}" if incident.innermost_scio_frame else "",
            f"occurrences {incident.occurrences}",
            f"issue #{incident.issue_number}" if incident.issue_number is not None else "",
            f"PR #{incident.pr_number}" if incident.pr_number is not None else "",
            f"ledger {incident.ledger_id}" if incident.ledger_id else "",
        ]
        print("  " + ", ".join(f for f in facts if f))
        if incident.escalation:
            print(f"  escalation: {incident.escalation}")
    return 0


def _watch(args: argparse.Namespace) -> int:
    store = Store.at_home(_home())
    try:
        if store.get_incident(args.incident) is None:
            _err(f"recovery watch: no incident {args.incident} (python -m recovery status)")
            return 2
        if not args.follow:
            for line in watch.render(store, args.incident, last=args.last):
                print(line)
            return 0
        try:
            watch.follow(
                store, args.incident, out=sys.stdout, last=args.last, interval_s=args.interval
            )
        except KeyboardInterrupt:
            print("stopped watching")
        return 0
    finally:
        store.close()


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _run,
    "wait": _wait,
    "tick": _tick,
    "status": _status,
    "incidents": _incidents,
    "hold": _hold,
    "release": _release,
    "dismiss": _dismiss,
    "watch": _watch,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except Exception as exc:
        _err(f"recovery {args.command}: {type(exc).__name__}: {exc}")
        return 2
