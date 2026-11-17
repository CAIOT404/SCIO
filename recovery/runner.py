"""The supervised run: lease, launch, follow, hang detection and the exit record.
OWNER: wave-1 agent A."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from recovery import procs
from recovery.commands import Runner, kill_tree, run_command
from recovery.config import HOME_ENV, WorkspaceConfig, recorded_env
from recovery.evidence import read_outcome
from recovery.model import ProcessKey, RunRecord, RunStatus, ThreadSource
from recovery.store import Store

OUTCOME_SUBCOMMANDS: tuple[str, ...] = ("build", "new", "run", "resume")
"""SCIO subcommands that accept ``--outcome-file``. The supervisor appends it only to these."""

OUTCOME_FLAG = "--outcome-file"


def outcome_flag_supported(live_wt: Path) -> bool:
    """Whether the SCIO that live-wt holds knows ``--outcome-file`` (ADR 0019 decision 11).

    A SCIO older than the flag refuses it with exit 2 before doing anything (I-187). So
    the flag is appended only when ``<live_wt>/scio/cli.py`` names it. Without it the
    supervisor reads the run's text instead, the same fallback it uses when a broken SCIO
    writes no outcome file. This reads a file and starts no process, because the check
    runs before every launch."""
    try:
        return OUTCOME_FLAG in (live_wt / "scio" / "cli.py").read_text(encoding="utf-8")
    except OSError:
        return False


PRODUCT_FLAGS: tuple[str, ...] = ("--product", "--dir")
"""The SCIO flags that name the product directory (``build``/``status`` and ``new``)."""

KILL_WAIT_S = 60.0
"""How long to wait for a killed child before killing it directly."""

SLEEP_GAP_S = 300.0
"""Two consecutive iterations of the poll loop further apart than this mean the machine
was down in between (asleep, hibernating): the supervisor was not scheduled, and neither
was its child (I-240). An iteration is one poll wait (15 s by default) plus a few file
reads and one store write, so a running machine never comes near it."""

SLEEP_GAP_POLLS = 4.0
"""The gap is never less than this many poll waits, for a workspace with a long
``poll_s``."""

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
"""``SetThreadExecutionState`` flags (winbase.h). ``ES_SYSTEM_REQUIRED`` resets the
system idle timer; with ``ES_CONTINUOUS`` the state stays until the next call that uses
``ES_CONTINUOUS``. ``ES_CONTINUOUS`` alone clears it."""


def sleep_gap_s(poll_s: float) -> float:
    """The time between two poll-loop iterations above which the machine was down."""
    return max(SLEEP_GAP_S, SLEEP_GAP_POLLS * poll_s)


def idle_s(now: float, since: float, gaps: Sequence[tuple[float, float]]) -> float:
    """Seconds without progress that the supervisor observed: ``now - since`` less the
    part of each downtime gap ``(start, end)`` that lies after ``since`` (the last
    progress, or the run's start). A gap that ended before the last progress excuses
    nothing, so a hang after a sleep is killed after the usual limit of awake time."""
    down = sum(max(0.0, end - max(start, since)) for start, end in gaps)
    return now - since - down


def set_execution_state(flags: int) -> None:
    """Call ``SetThreadExecutionState(flags)`` for the calling thread. A no-op off
    Windows. Raises ``OSError`` when the call fails (it returns NULL).

    The request belongs to the calling thread and ends with it, so the thread that polls
    the child makes it. It keeps the system from sleeping on its idle timer. It does not
    stop a sleep the user asks for (the lid, the power button, the Start menu): the poll
    loop's downtime gap covers that one."""
    # A platform block, as in procs.py: mypy on Linux skips it. An early return made mypy
    # there call the rest unreachable, which only CI saw (the gate runs on Windows).
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        call = kernel32.SetThreadExecutionState
        call.argtypes = (ctypes.c_uint,)
        call.restype = ctypes.c_uint
        if not call(flags):
            raise OSError(f"SetThreadExecutionState({flags:#010x}) failed")


@contextlib.contextmanager
def stay_awake(api: Callable[[int], object] = set_execution_state) -> Iterator[None]:
    """Ask the system to stay awake inside the block, and release the request after it,
    also when the block raises. A request that fails never stops the run: one line on
    stderr, and nothing to release."""
    held = _execution_state(api, ES_CONTINUOUS | ES_SYSTEM_REQUIRED, "stay awake")
    try:
        yield
    finally:
        if held:
            _execution_state(api, ES_CONTINUOUS, "release its stay awake request")


def _execution_state(api: Callable[[int], object], flags: int, what: str) -> bool:
    try:
        api(flags)
    except Exception as exc:
        print(f"recovery: could not ask the system to {what}: {exc!r}", file=sys.stderr)
        return False
    return True


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    """``run-<UTC yyyymmddHHMMSS>-<6 hex>``."""
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(clock()))
    return f"run-{stamp}-{secrets.token_hex(3)}"


def progress_time(workspace: Path, run: RunRecord) -> float:
    """The newest modification time among the workspace's ``.scio/ledger/*.jsonl`` and
    ``.scio/checkpoints.sqlite-wal`` and the run's own output files. 0.0 when none exist."""
    scio = workspace / ".scio"
    paths = [*(scio / "ledger").glob("*.jsonl"), scio / "checkpoints.sqlite-wal"]
    paths += [Path(p) for p in (run.stdout_path, run.stderr_path) if p]
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def product_dir(workspace: Path, argv: Sequence[str]) -> Path | None:
    """The product directory named by ``--product X`` or ``--dir X`` (or ``--flag=X``),
    resolved against the workspace, the launcher's working directory."""
    for i, arg in enumerate(argv):
        value: str | None = None
        if arg in PRODUCT_FLAGS and i + 1 < len(argv):
            value = argv[i + 1]
        else:
            flag, eq, rest = arg.partition("=")
            if eq and flag in PRODUCT_FLAGS:
                value = rest
        if value:
            path = Path(value)
            return path if path.is_absolute() else workspace / path
    return None


def build_thread(product: Path | None) -> str:
    """``build_thread`` from ``<product>/.scio/product/progress.json``, or "".

    That is the thread that recorded the product's last finished milestone, not always the
    one ``scio build`` runs next (I-163), so a run record holds it only as a provisional
    guess until :func:`recorded_thread` reads the thread SCIO ran (issue #123)."""
    if product is None:
        return ""
    try:
        data = json.loads(
            (product / ".scio" / "product" / "progress.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    value = data.get("build_thread") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


THREAD_LINE = re.compile(r"^(?:build |product )?thread (?P<thread>\S+) phase=")
"""The first line ``scio build``, ``scio new`` and ``scio run``/``resume`` print when a run
ends (``build thread build-x-m3 phase=merge_wait exit=None dir ...``), as
``evidence._HEADER`` reads it. ``scio status``'s ``build thread <id>`` names
``progress.json``'s thread and has no ``phase=``, so it never matches."""


def printed_thread(stdout_path: Path) -> str:
    """The thread of the last :data:`THREAD_LINE` in SCIO's stdout, or "".

    The whole file is read, not a tail: a pause prints its interrupt JSON after the line.
    Bytes that are not UTF-8 are replaced (SCIO's output through ``sh`` on Windows is
    cp1252), so one stray byte never hides the line."""
    found = ""
    try:
        with stdout_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if (m := THREAD_LINE.match(line)) and m["thread"] not in ("-", "None"):
                    found = m["thread"]
    except OSError:
        return ""
    return found


FEATURE_SUFFIX = re.compile(r"-p\d+-a\d+$")
"""A feature run's suffix on its build thread: ``build-x-m3-p26-a4`` runs in ``build-x-m3``
(``scio/product/build.py``: ``f"{thread}-{feature id}-a{attempt}"``, ids ``P<n>``)."""

LEDGER_THREAD = re.compile(r"^(?:build-.+-m\d+(?:-r\d+)?|new-[0-9a-f]+)$")
"""A ledger name, its feature suffix stripped, that is a thread SCIO's CLI starts: a build
thread (``build-<product>-m<n>``, ``-r<n>`` after a halt, ADR 0011) or a product thread
(``new-<hex>``). A task thread's id is free text, so a task's ledger is never read."""


def ledger_thread(workspace: Path, since: float) -> str:
    """The thread of the newest ``<workspace>/.scio/ledger/*.jsonl`` written at or after
    ``since``, or "".

    SCIO writes each thread's cost ledger to ``.scio/ledger/<thread id>.jsonl`` under its
    working directory, the workspace (``scio/graph/run_context.py``). A feature's ledger
    names its build thread once :data:`FEATURE_SUFFIX` is stripped. Names that are not a
    thread (:data:`LEDGER_THREAD`) and files older than ``since`` are ignored. ``since`` is
    a run's ``started_at``: ``time.time()``, seconds since the epoch, the same clock as a
    file's ``st_mtime``, so the two compare without any time-zone conversion."""
    newest, found = 0.0, ""
    for path in (workspace / ".scio" / "ledger").glob("*.jsonl"):
        thread = FEATURE_SUFFIX.sub("", path.stem)
        if not LEDGER_THREAD.match(thread):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since and (not found or mtime > newest):
            newest, found = mtime, thread
    return found


def recorded_thread(run: RunRecord, product: Path | None) -> tuple[str, ThreadSource]:
    """The thread SCIO ran in ``run``, and where it was read (issue #123):

    1. ``outcome``: the ``thread`` of SCIO's outcome file, when it is a non-empty string
       (it is null when SCIO refused before picking one);
    2. ``stdout``: else the last thread line SCIO printed (:func:`printed_thread`), for a
       SCIO older than ``--outcome-file`` or a run killed between the line and the file;
    3. ``ledger``: else the thread of the newest cost ledger the run wrote
       (:func:`ledger_thread`). SCIO prints its thread line only when a run ends, so this
       is what names the thread of a build killed as hung;
    4. ``progress``: else ``progress.json``'s ``build_thread`` (:func:`build_thread`).

    ``("", "progress")`` when none names one. Never raises on a missing or broken file."""
    outcome = read_outcome(Path(run.outcome_path)) if run.outcome_path else None
    thread = outcome.get("thread") if outcome is not None else None
    if isinstance(thread, str) and thread.strip():
        return thread.strip(), "outcome"
    printed = printed_thread(Path(run.stdout_path)) if run.stdout_path else ""
    if printed:
        return printed, "stdout"
    written = ledger_thread(Path(run.workspace), run.started_at) if run.workspace else ""
    if written:
        return written, "ledger"
    return build_thread(product), "progress"


def _record_thread(run: RunRecord, product: Path | None) -> None:
    """Replace the run's provisional thread by :func:`recorded_thread`'s, when it names one.
    Every path that ends a run calls it before the final save."""
    thread, source = recorded_thread(run, product)
    if thread:
        run.build_thread, run.build_thread_source = thread, source


def _live_wt_head(cfg: WorkspaceConfig, git: Runner) -> str:
    result = git(["git", "-C", str(cfg.live_wt), "rev-parse", "HEAD"], timeout_s=60)
    return result.stdout.strip() if result.ok else ""


def _wait(popen: subprocess.Popen[bytes]) -> int:
    try:
        return popen.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        popen.kill()
        return popen.wait()


def supervise(
    cfg: WorkspaceConfig,
    store: Store,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    home: Path,
    incident_id: str | None = None,
    resumed_from: str | None = None,
    echo: bool = True,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    spawn: Callable[..., subprocess.Popen[bytes]] = procs.spawn,
    alive: Callable[[ProcessKey], bool] = procs.alive,
    kill: Callable[[int], None] = kill_tree,
    git: Runner = run_command,
    awake: Callable[[int], object] = set_execution_state,
) -> RunRecord:
    """Run ``cfg.launcher + argv`` in ``cfg.workspace`` as this process's child and return
    its finished record.

    - **Lease.** Takes the workspace lease (``cfg.lease_name``). If a live process holds
      it, the record is saved as ``REFUSED`` and returned without starting anything.
    - **Run record.** Records the run before starting it: the ``scio_sha`` (live-wt's
      HEAD), ``product_dir`` (from ``--product`` or ``--dir``), a provisional build thread
      (``progress.json``'s, source ``progress``), ``run_dir`` under ``home/runs/<run_id>``,
      and the stdout, stderr and outcome paths.
    - **Outcome file.** Appends ``--outcome-file <path>`` when ``argv[0]`` is in
      ``OUTCOME_SUBCOMMANDS``.
    - **While running.** Every ``cfg.limits.poll_s`` seconds it echoes new output to its
      own stdout and stderr (when ``echo``), updates ``last_progress_at`` from
      :func:`progress_time`, and kills the tree (status ``KILLED_HANG``) after
      ``cfg.limits.hang_after_s`` with no progress. Time the machine was down is not
      idle time (I-240): two iterations more than :func:`sleep_gap_s` apart add the gap
      to ``downtime_s`` with a note, and :func:`idle_s` leaves it out.
    - **Awake.** ``awake`` (``SetThreadExecutionState``) is asked to keep the system
      awake from before the spawn until the child has ended (:func:`stay_awake`).
    - **At exit.** Records the exit code, ``ended_at`` and status ``EXITED``, then releases
      the lease.

    The child gets ``env`` whole; the record keeps only ``config.recorded_env(env)``. A
    launcher that cannot start is recorded as exit 127, with the reason in stderr.log. A
    ``KeyboardInterrupt`` kills the tree, records the exit, releases the lease and is
    re-raised. Every exit records the thread SCIO ran, from :func:`recorded_thread`: the
    outcome file, else SCIO's thread line, else the ledger the run wrote, else
    ``progress.json`` again (issue #123).
    """
    args = list(argv)
    me = procs.self_key()
    run_id = new_run_id(clock)
    run_dir = home / "runs" / run_id
    product = product_dir(cfg.workspace, args)
    run = RunRecord(
        run_id=run_id,
        workspace=str(cfg.workspace),
        launcher=list(cfg.launcher),
        argv=args,
        env=recorded_env(env),
        started_at=clock(),
        scio_sha=_live_wt_head(cfg, git),
        product_dir=str(product) if product is not None else "",
        build_thread=build_thread(product),
        build_thread_source="progress",
        supervisor_pid=me.pid,
        supervisor_created=me.created,
        run_dir=str(run_dir),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        outcome_path=str(run_dir / "outcome.json"),
        incident_id=incident_id,
        resumed_from=resumed_from,
    )
    if not store.acquire_lease(cfg.lease_name, me, alive, note=run_id):
        run.status = RunStatus.REFUSED
        run.ended_at = clock()
        store.save_run(run)
        return run
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save_run(run)
        # This thread polls the child until it ends, so the request lives as long as it.
        with stay_awake(awake):
            return _supervise_child(
                cfg,
                store,
                run,
                product,
                env=env,
                echo=echo,
                clock=clock,
                sleep=sleep,
                spawn=spawn,
                kill=kill,
            )
    finally:
        store.release_lease(cfg.lease_name, me)


def _supervise_child(
    cfg: WorkspaceConfig,
    store: Store,
    run: RunRecord,
    product: Path | None,
    *,
    env: Mapping[str, str],
    echo: bool,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    spawn: Callable[..., subprocess.Popen[bytes]],
    kill: Callable[[int], None],
) -> RunRecord:
    command = list(cfg.launcher) + run.argv
    if run.argv and run.argv[0] in OUTCOME_SUBCOMMANDS and outcome_flag_supported(cfg.live_wt):
        command += ["--outcome-file", run.outcome_path]
    stdout_path, stderr_path = Path(run.stdout_path), Path(run.stderr_path)
    streams = [(stdout_path, sys.stdout), (stderr_path, sys.stderr)]
    offsets = [0, 0]
    limits = cfg.limits
    popen: subprocess.Popen[bytes] | None = None
    try:
        try:
            popen = spawn(
                command,
                cwd=cfg.workspace,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except OSError as exc:
            with stderr_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"recovery: cannot start {command[0]}: {exc}\n")
            if echo:
                procs.follow(streams, offsets)
            run.exit_code = 127
            run.ended_at = clock()
            run.status = RunStatus.EXITED
            _record_thread(run, product)
            store.save_run(run)
            return run
        key = procs.process_key(popen.pid)
        run.pid = popen.pid
        run.pid_created = key.created if key is not None else 0.0
        store.save_run(run)
        gap_s = sleep_gap_s(limits.poll_s)
        gaps: list[tuple[float, float]] = []
        seen = run.started_at
        while True:
            # One clock reading per iteration: wherever the machine went down, the gap
            # lies between two readings, and the hang check below uses the same one.
            now = clock()
            if now - seen > gap_s:
                gaps.append((seen, now))
                run.downtime_s += now - seen
                run.notes.append(
                    f"machine was down {(now - seen) / 3600:.1f} h; not counted as idle"
                )
                store.save_run(run)
            seen = now
            code = popen.poll()
            if echo:
                offsets = procs.follow(streams, offsets)
            if code is not None:
                break
            progress = progress_time(cfg.workspace, run)
            if progress > run.last_progress_at:
                run.last_progress_at = progress
                store.save_run(run)
            since = max(run.last_progress_at, run.started_at)
            if idle_s(now, since, gaps) > limits.hang_after_s:
                kill(popen.pid)
                code = _wait(popen)
                run.status = RunStatus.KILLED_HANG
                break
            sleep(limits.poll_s)
    except KeyboardInterrupt:
        if popen is not None:
            kill(popen.pid)
            run.exit_code = _wait(popen)
        run.ended_at = clock()
        run.status = RunStatus.EXITED
        _record_thread(run, product)
        store.save_run(run)
        raise
    if echo:
        procs.follow(streams, offsets)
    run.exit_code = code
    run.ended_at = clock()
    if run.status is RunStatus.RUNNING:
        run.status = RunStatus.EXITED
    run.last_progress_at = max(run.last_progress_at, progress_time(cfg.workspace, run))
    _record_thread(run, product)
    store.save_run(run)
    return run


def find_orphans(
    store: Store, *, alive: Callable[[ProcessKey], bool] = procs.alive
) -> list[RunRecord]:
    """Runs still ``RUNNING`` whose child process and supervisor process are both gone.

    Each one is marked ``ORPHANED``, saved and returned. A run whose supervisor died
    while its child still runs is left alone: the child may finish on its own, and a
    later tick looks again. The supervisor never reached the run's end, so its thread is
    recorded here the way an ending run records it (:func:`recorded_thread`).
    """
    orphans: list[RunRecord] = []
    for run in store.runs(status=RunStatus.RUNNING):
        supervisor = ProcessKey(run.supervisor_pid, run.supervisor_created)
        if alive(run.process) or alive(supervisor):
            continue
        run.status = RunStatus.ORPHANED
        _record_thread(run, Path(run.product_dir) if run.product_dir else None)
        store.save_run(run)
        orphans.append(run)
    return orphans


def replay_argv(run: RunRecord, incident_id: str, *, python: str = sys.executable) -> list[str]:
    """The argv a tick starts to resume ``run`` under a fresh supervisor:

    ``[python, "-m", "recovery", "run", "--workspace", run.workspace,
    "--incident", incident_id, "--resumed-from", run.run_id, "--", *run.argv]``
    """
    return [
        python,
        "-m",
        "recovery",
        "run",
        "--workspace",
        run.workspace,
        "--incident",
        incident_id,
        "--resumed-from",
        run.run_id,
        "--",
        *run.argv,
    ]


def start_replay(
    run: RunRecord,
    incident_id: str,
    *,
    home: Path,
    package_root: Path,
    spawn_detached: Callable[..., int] = procs.spawn_detached,
) -> int:
    """Start :func:`replay_argv` detached, with ``cwd=package_root`` (the directory that
    holds ``recovery/``). The environment is ``run.env`` plus ``SCIO_RECOVERY_HOME=home``,
    and the log goes to ``home/replays/<incident_id>.log``. Returns the PID."""
    env = dict(run.env)
    env[HOME_ENV] = str(home)
    return spawn_detached(
        replay_argv(run, incident_id),
        cwd=package_root,
        env=env,
        log_path=home / "replays" / f"{incident_id}.log",
    )
