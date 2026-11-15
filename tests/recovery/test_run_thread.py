"""The run record names the thread SCIO ran, not the one progress.json recorded (issue #123).

``progress.json``'s ``build_thread`` is the thread that recorded the last *finished*
milestone. SCIO's CLI picks the thread to run by another rule (I-163), so the two disagree
whenever a build moves to a new thread: the graphcalc runs of 2026-11-14 ran
``build-graphcalc-m3`` while the supervisor recorded ``build-graphcalc-m2``. At the end of a
run the record takes the thread from SCIO's outcome file, else from the last
``build thread <id> phase=...`` line SCIO printed, else from ``progress.json``; the value
recorded at the start comes from ``progress.json`` and is marked provisional."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from recovery import procs
from recovery.commands import CommandResult
from recovery.config import Limits, WorkspaceConfig
from recovery.model import ProcessKey, RunRecord, RunStatus
from recovery.runner import find_orphans, supervise
from recovery.store import Store

FINISHED, RUNNING = "build-prod-m2", "build-prod-m3"
"""progress.json's thread (it recorded the finished M2) and the one SCIO runs now."""

THREAD_LINE_TEXT = "build thread "

STUB = r"""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
mode = os.environ["STUB_MODE"]
thread = os.environ.get("STUB_THREAD") or None
if mode == "outcome":
    if thread:
        print(f"build thread {thread} phase=merge_wait exit=None dir P", flush=True)
    path = Path(args[args.index("--outcome-file") + 1])
    data = dict(version=1, command="build", status="paused", exit_code=0, thread=thread,
                exit_reason=None, reason=None, resume=None, exception=None)
    path.write_text(json.dumps(data), encoding="utf-8")
elif mode == "header-then-hang":
    out = sys.stdout.buffer
    # scio status's line names progress.json's thread, with no phase: never the run's
    out.write(b"product prod (stack python) bootstrap - build thread build-prod-m2\n")
    out.write(f"build thread {thread} phase=feature exit=None dir P\n".encode())
    # a cp1252 middle dot, as SCIO prints it through sh on Windows: not UTF-8
    out.write(b"milestones done - \xb7 current M3\n")
    out.flush()
    time.sleep(120)
elif mode == "ledger-then-hang":
    # a build that hangs mid-feature: its cost ledger is written, its thread never printed
    ledger = Path(os.environ["STUB_LEDGER"])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{}\n", encoding="utf-8")
    time.sleep(120)
elif mode == "quiet":
    print("hello from scio", flush=True)
"""

NORMAL_EXIT_HANG_S = 60.0
HANG_S = 3.0
"""Long enough for a loaded machine to start the stub's python before the hang clock runs out."""


class FakeGit:
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return CommandResult(0, "abc123\n", "")


def _cfg(tmp_path: Path, *, hang_after_s: float = NORMAL_EXIT_HANG_S) -> WorkspaceConfig:
    stub = tmp_path / "stub_scio.py"
    stub.write_text(STUB, encoding="utf-8", newline="\n")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    repo = tmp_path / "SCIO"
    cli = repo / ".scio" / "live-wt" / "scio" / "cli.py"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text('FLAGS = ["--outcome-file"]\n', encoding="utf-8", newline="\n")
    return WorkspaceConfig(
        workspace=ws,
        launcher=(sys.executable, str(stub)),
        scio_repo=repo,
        scio_gh_repo="o/SCIO",
        live_wt=repo / ".scio" / "live-wt",
        limits=Limits(poll_s=0.1, hang_after_s=hang_after_s),
    )


def _progress(cfg: WorkspaceConfig) -> Path:
    """``prod``'s progress.json: M2 done, recorded by ``build-prod-m2``."""
    path = cfg.workspace / "prod" / ".scio" / "product" / "progress.json"
    path.parent.mkdir(parents=True)
    data = {"product": "prod", "build_thread": FINISHED, "milestones": [{"id": "M2"}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _ledger(cfg: WorkspaceConfig, name: str) -> Path:
    return cfg.workspace / ".scio" / "ledger" / name


def _supervise(
    cfg: WorkspaceConfig,
    store: Store,
    home: Path,
    mode: str,
    thread: str = "",
    *,
    ledger: Path | None = None,
    **kw: Any,
) -> RunRecord:
    env = dict(os.environ)
    env.update(STUB_MODE=mode, STUB_THREAD=thread, STUB_LEDGER=str(ledger or ""))
    argv = ["build", "--product", "prod", "--gh", "o/p"]
    return supervise(cfg, store, argv, env=env, home=home, echo=False, git=FakeGit(), **kw)


def _source(run: RunRecord) -> str:
    """The new field, read so that a record without it fails the assertion, not the import."""
    return str(getattr(run, "build_thread_source", "<no build_thread_source>"))


def test_the_run_records_the_thread_scio_ran_not_progress_jsons(tmp_path: Path) -> None:
    cfg, store, home = _cfg(tmp_path), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    _progress(cfg)
    before_spawn: list[RunRecord] = []

    def spawn(*args: Any, **kw: Any) -> subprocess.Popen[bytes]:
        before_spawn.extend(store.runs(status=RunStatus.RUNNING))
        return procs.spawn(*args, **kw)

    run = _supervise(cfg, store, home, "outcome", RUNNING, spawn=spawn)
    assert json.loads(Path(run.outcome_path).read_text(encoding="utf-8"))["thread"] == RUNNING
    assert (run.status, run.exit_code) == (RunStatus.EXITED, 0)
    assert (run.build_thread, _source(run)) == (RUNNING, "outcome")
    saved = store.get_run(run.run_id)
    assert saved is not None and (saved.build_thread, _source(saved)) == (RUNNING, "outcome")
    # before SCIO runs, progress.json's thread is all there is, and the record says so
    assert [(r.build_thread, _source(r)) for r in before_spawn] == [(FINISHED, "progress")]


def test_a_run_killed_before_its_outcome_file_takes_the_thread_from_its_output(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, hang_after_s=HANG_S)
    store, home = Store(tmp_path / "s.sqlite"), tmp_path / "home"
    _progress(cfg)
    run = _supervise(cfg, store, home, "header-then-hang", RUNNING)
    assert run.status is RunStatus.KILLED_HANG
    assert not Path(run.outcome_path).exists()
    assert (run.build_thread, _source(run)) == (RUNNING, "stdout")
    saved = store.get_run(run.run_id)
    assert saved is not None and (saved.build_thread, _source(saved)) == (RUNNING, "stdout")


def test_a_hung_run_takes_the_thread_from_the_ledger_it_wrote(tmp_path: Path) -> None:
    """Real SCIO prints its thread line only when a run ends, so a build killed as hung
    leaves neither the line nor the outcome file: the ledger it wrote names its thread."""
    cfg = _cfg(tmp_path, hang_after_s=HANG_S)
    store, home = Store(tmp_path / "s.sqlite"), tmp_path / "home"
    _progress(cfg)
    older = _ledger(cfg, f"{FINISHED}-p9-a1.jsonl")
    older.parent.mkdir(parents=True)
    older.write_text("{}\n", encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(older, (long_ago, long_ago))
    ledger = _ledger(cfg, f"{RUNNING}-p1-a1.jsonl")
    run = _supervise(cfg, store, home, "ledger-then-hang", ledger=ledger)
    assert run.status is RunStatus.KILLED_HANG
    assert ledger.is_file() and not Path(run.outcome_path).exists()
    assert THREAD_LINE_TEXT not in Path(run.stdout_path).read_text(encoding="utf-8")
    assert (run.build_thread, _source(run)) == (RUNNING, "ledger")
    saved = store.get_run(run.run_id)
    assert saved is not None and (saved.build_thread, _source(saved)) == (RUNNING, "ledger")


def test_a_ledger_older_than_the_run_is_ignored(tmp_path: Path) -> None:
    """A ledger from an earlier run names that run's thread, not this one's."""
    cfg, store, home = _cfg(tmp_path), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    _progress(cfg)
    older = _ledger(cfg, f"{RUNNING}-p1-a1.jsonl")
    older.parent.mkdir(parents=True)
    older.write_text("{}\n", encoding="utf-8")
    before = time.time() - 120
    os.utime(older, (before, before))
    run = _supervise(cfg, store, home, "quiet")
    assert run.started_at > before
    assert (run.build_thread, _source(run)) == (FINISHED, "progress")


def test_the_ledger_counts_from_the_runs_start_on_the_runs_clock(tmp_path: Path) -> None:
    """``started_at`` is ``time.time()``, seconds since the epoch, the clock of a file's
    mtime: a ledger one second before the start is ignored and one second after counts,
    whatever the machine's time zone. Names that are not a thread never count."""
    from recovery.runner import ledger_thread

    ws = tmp_path / "ws"
    ledgers = ws / ".scio" / "ledger"
    ledgers.mkdir(parents=True)
    started = time.time()
    files = {
        f"{FINISHED}-p9-a1.jsonl": started - 1.0,  # before the start
        f"{RUNNING}-p1-a1.jsonl": started + 1.0,  # a feature of the run's build thread
        f"{RUNNING}-r2.jsonl": started + 2.0,  # the build thread after a halt
        "t1.jsonl": started + 3.0,  # a task thread's id is free text
        "notes.jsonl": started + 4.0,
        "build-prod-m4.txt": started + 5.0,  # not a ledger
    }
    for name, mtime in files.items():
        (ledgers / name).write_text("{}\n", encoding="utf-8")
        os.utime(ledgers / name, (mtime, mtime))
    assert ledger_thread(ws, started) == f"{RUNNING}-r2"
    assert ledger_thread(ws, started - 0.5) == f"{RUNNING}-r2"
    os.utime(ledgers / f"{RUNNING}-r2.jsonl", (started - 2.0, started - 2.0))
    assert ledger_thread(ws, started) == RUNNING  # -p1-a1 stripped
    assert ledger_thread(ws, started + 1.5) == ""  # only non-threads are newer
    assert ledger_thread(ws, started - 1.5) == RUNNING
    assert ledger_thread(tmp_path / "no-workspace", started) == ""


@pytest.mark.parametrize(
    "mode",
    [
        "quiet",  # no outcome file (a SCIO older than the flag) and no thread line
        "outcome",  # an outcome file whose thread is null (the two-threads refusal)
    ],
)
def test_a_run_without_either_falls_back_to_progress_json(tmp_path: Path, mode: str) -> None:
    cfg, store, home = _cfg(tmp_path), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    _progress(cfg)
    run = _supervise(cfg, store, home, mode)
    assert (run.status, run.exit_code) == (RunStatus.EXITED, 0)
    assert Path(run.outcome_path).exists() is (mode == "outcome")
    assert (run.build_thread, _source(run)) == (FINISHED, "progress")


def test_an_old_run_record_without_the_source_field_loads(tmp_path: Path) -> None:
    """A record saved before the field existed (the live store's rows of 2026-11-14) loads,
    with the source its thread did come from: progress.json."""
    old: dict[str, Any] = {
        "run_id": "run-20261114194904-20a08c",
        "workspace": "W",
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P", "--decision", "retry"],
        "env": {"PATH": "/bin"},
        "started_at": 1794685744.9,
        "status": "exited",
        "scio_sha": "fe83a50",
        "product_dir": "P",
        "build_thread": "build-graphcalc-m2",
        "exit_code": 0,
        "ended_at": 1794690267.8,
    }
    record = RunRecord.from_dict(old)
    assert (record.build_thread, _source(record)) == ("build-graphcalc-m2", "progress")
    path = tmp_path / "state.sqlite"
    Store(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO runs(run_id, workspace, status, incident_id, started_at, data)"
            " VALUES(?,?,?,?,?,?)",
            (old["run_id"], "W", "exited", None, old["started_at"], json.dumps(old)),
        )
    conn.close()
    store = Store(path)
    saved = store.get_run(old["run_id"])
    assert saved is not None
    assert (saved.build_thread, _source(saved)) == ("build-graphcalc-m2", "progress")
    store.save_run(saved)  # and it saves again with the field
    again = store.get_run(old["run_id"])
    assert again is not None and again.to_dict() == saved.to_dict()


def test_an_orphan_records_the_thread_its_outcome_file_names(tmp_path: Path) -> None:
    """The tick marks a run orphaned when its supervisor and child are gone; the supervisor
    never reached the run's end, so the tick records the thread the same way."""
    cfg = _cfg(tmp_path)
    _progress(cfg)
    run_dir = tmp_path / "home" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    outcome = run_dir / "outcome.json"
    outcome.write_text(json.dumps({"version": 1, "thread": RUNNING}), encoding="utf-8")
    store = Store(tmp_path / "s.sqlite")
    store.save_run(
        RunRecord(
            run_id="run-1",
            workspace=str(cfg.workspace),
            launcher=["sh", "scio.sh"],
            argv=["build", "--product", "prod"],
            env={},
            started_at=100.0,
            product_dir=str(cfg.workspace / "prod"),
            build_thread=FINISHED,
            pid=1,
            pid_created=10.0,
            supervisor_pid=2,
            supervisor_created=20.0,
            run_dir=str(run_dir),
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
            outcome_path=str(outcome),
        )
    )

    def alive(key: ProcessKey) -> bool:
        return False

    [orphan] = find_orphans(store, alive=alive)
    assert orphan.status is RunStatus.ORPHANED
    assert (orphan.build_thread, _source(orphan)) == (RUNNING, "outcome")
    saved = store.get_run("run-1")
    assert saved is not None and (saved.build_thread, _source(saved)) == (RUNNING, "outcome")
