"""Issue #175: ``run --detach`` starts a supervised build that no session owns, and
``wait`` reports its stop from a command that is safe to kill.

Every test but the last replaces the detached spawn, so no process starts. The store is the
real one under the test's SCIO_RECOVERY_HOME (conftest), never the machine's."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from recovery import cli, machine, procs, runner
from recovery.config import WorkspaceConfig
from recovery.model import (
    FailureClass,
    Incident,
    IncidentState,
    ProcessKey,
    RunRecord,
    RunStatus,
)
from recovery.store import Store

BUILD = ["build", "--product", "P"]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "recovery.toml").write_text(
        'launcher = ["sh", "scio.sh"]\nscio_repo = "C:/p/SCIO"\nscio_gh_repo = "o/SCIO"\n',
        encoding="utf-8",
        newline="\n",
    )
    return ws


def _record(run_id: str, workspace: Path, home: Path, **kw: Any) -> RunRecord:
    run_dir = home / "runs" / run_id
    values: dict[str, Any] = {
        "run_id": run_id,
        "workspace": str(workspace.resolve()),
        "launcher": ["sh", "scio.sh"],
        "argv": list(BUILD),
        "env": {},
        "started_at": 1_800_000_000.0,
        "run_dir": str(run_dir),
        "stdout_path": str(run_dir / "stdout.log"),
        "stderr_path": str(run_dir / "stderr.log"),
    }
    values.update(kw)
    return RunRecord(**values)


def _save(home: Path, *records: RunRecord) -> None:
    store = Store.at_home(home)
    try:
        for record in records:
            store.save_run(record)
    finally:
        store.close()


def _stdout(record: RunRecord, text: str) -> None:
    path = Path(record.stdout_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class Detached:
    """Replaces ``procs.spawn_detached`` by what the detached supervisor does first: it
    saves the run's record under the id it was given. ``runner.supervise`` and the machine
    are replaced too, so a call that reaches them is seen and starts nothing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        self.home = home
        self.spawned: list[dict[str, Any]] = []
        self.supervise_calls: list[dict[str, Any]] = []
        self.error: OSError | None = None
        self.records = True
        monkeypatch.setattr(procs, "spawn_detached", self.spawn_detached)
        monkeypatch.setattr(runner, "supervise", self.supervise)
        monkeypatch.setattr(machine, "on_run_finished", lambda run, deps: None)

    def spawn_detached(self, argv: Any, *, cwd: Path, env: Any, log_path: Path) -> int:
        self.spawned.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "log": log_path})
        if self.error is not None:
            raise self.error
        if self.records:
            words = list(argv)
            workspace = Path(words[words.index("--workspace") + 1])
            run_id = words[words.index("--run-id") + 1]
            _save(self.home, _record(run_id, workspace, self.home, supervisor_pid=4242))
        return 4242

    def supervise(self, cfg: WorkspaceConfig, store: Store, argv: Any, **kw: Any) -> RunRecord:
        self.supervise_calls.append({"cfg": cfg, "argv": list(argv), **kw})
        record = _record("run-20270101000000-aaaaaa", cfg.workspace, self.home)
        record.status, record.exit_code = RunStatus.EXITED, 0
        store.save_run(record)
        return record


@pytest.fixture
def det(monkeypatch: pytest.MonkeyPatch, recovery_home: Path) -> Detached:
    return Detached(monkeypatch, recovery_home)


def _runs(home: Path) -> list[RunRecord]:
    store = Store.at_home(home)
    try:
        return store.runs()
    finally:
        store.close()


# run --detach


def test_run_detach_starts_the_supervised_run_detached_and_returns_at_once(
    workspace: Path, det: Detached, recovery_home: Path
) -> None:
    started = time.monotonic()
    argv = ["run", "--detach", "--workspace", str(workspace), "--", *BUILD]
    assert cli.main(argv) == 0
    assert time.monotonic() - started < cli.DETACH_WAIT_S / 2
    # The supervised run is not run here: it is handed to spawn_detached, whole.
    assert det.supervise_calls == []
    [spawn] = det.spawned
    words = spawn["argv"]
    run_id = words[words.index("--run-id") + 1]
    assert words == [
        sys.executable,
        "-m",
        "recovery",
        "run",
        "--workspace",
        str(workspace.resolve()),
        "--run-id",
        run_id,
        "--",
        *BUILD,
    ]
    assert "--detach" not in words  # the child is the ordinary, attached supervised run
    assert spawn["cwd"] == cli.PACKAGE_ROOT and (spawn["cwd"] / "recovery").is_dir()
    assert spawn["env"]["SCIO_RECOVERY_HOME"] == str(recovery_home)
    assert spawn["log"] == recovery_home / "runs" / run_id / "supervisor.log"
    # This process took no lease: the detached supervisor is the one that holds it.
    store = Store.at_home(recovery_home)
    try:
        assert store.lease_holder(WorkspaceConfig.load(workspace).lease_name) is None
    finally:
        store.close()


def test_run_detach_prints_the_run_id_and_log_path(
    workspace: Path, det: Detached, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "--detach", "--workspace", str(workspace), "--", *BUILD]) == 0
    [run] = _runs(recovery_home)
    assert cli.RUN_ID.match(run.run_id)
    out = capsys.readouterr()
    assert out.out.splitlines() == [
        f"recovery run: detached run {run.run_id} (pid 4242); stdout: {run.stdout_path}"
    ]
    assert run.stdout_path == str(recovery_home / "runs" / run.run_id / "stdout.log")
    assert out.err == ""


def test_run_detach_refuses_a_busy_workspace_like_an_attached_run(
    workspace: Path,
    det: Detached,
    recovery_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = WorkspaceConfig.load(workspace)
    me = procs.self_key()  # a live holder: this test process
    store = Store.at_home(recovery_home)
    try:
        assert store.acquire_lease(cfg.lease_name, me, procs.alive, note="run-busy")
    finally:
        store.close()
    busy = f"recovery run: {cfg.workspace} is busy: pid {me.pid} holds its lease; nothing started"

    assert cli.main(["run", "--detach", "--workspace", str(workspace), "--", *BUILD]) == 2
    detached = capsys.readouterr()
    assert detached.out == "" and detached.err.splitlines() == [busy]
    assert det.spawned == [] and _runs(recovery_home) == []  # nothing started

    # The attached run on the same busy workspace: the same line, the same exit code.
    def refused(cfg: WorkspaceConfig, store: Store, argv: Any, **kw: Any) -> RunRecord:
        record = _record("run-20270101000000-bbbbbb", cfg.workspace, recovery_home)
        record.status = RunStatus.REFUSED
        store.save_run(record)
        return record

    monkeypatch.setattr(runner, "supervise", refused)
    assert cli.main(["run", "--workspace", str(workspace), "--", *BUILD]) == 2
    attached = capsys.readouterr()
    assert attached.err.splitlines() == [busy] and det.spawned == []


def test_a_second_detach_that_loses_the_lease_race_is_refused_as_busy(
    workspace: Path,
    det: Detached,
    recovery_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both callers saw a free workspace; the second supervisor's record is ``refused``."""
    real = det.spawn_detached

    def lost(argv: Any, **kw: Any) -> int:
        pid = real(argv, **kw)
        [run] = _runs(recovery_home)
        run.status = RunStatus.REFUSED
        _save(recovery_home, run)
        return pid

    monkeypatch.setattr(procs, "spawn_detached", lost)
    assert cli.main(["run", "--detach", "--workspace", str(workspace), "--", *BUILD]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "is busy" in out.err and "nothing started" in out.err


def test_run_detach_that_cannot_start_exits_1_and_leaves_no_run(
    workspace: Path, det: Detached, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    det.error = FileNotFoundError("no python")
    assert cli.main(["run", "--detach", "--workspace", str(workspace), "--", *BUILD]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "did not start" in out.err and "no python" in out.err
    assert _runs(recovery_home) == []


def test_run_detach_says_so_when_no_run_record_appears(
    workspace: Path,
    det: Detached,
    recovery_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    det.records = False
    monkeypatch.setattr(cli, "DETACH_WAIT_S", 0.3)
    assert cli.main(["run", "--detach", "--workspace", str(workspace), "--", *BUILD]) == 1
    out = capsys.readouterr()
    run_id = det.spawned[0]["argv"][det.spawned[0]["argv"].index("--run-id") + 1]
    assert out.out == ""
    assert f"detached run {run_id} (pid 4242) recorded nothing" in out.err
    assert str(recovery_home / "runs" / run_id) in out.err
    assert _runs(recovery_home) == []  # no run left marked running


def test_an_attached_run_is_unchanged_by_the_detach_flag(
    workspace: Path, det: Detached, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "--workspace", str(workspace), "--", *BUILD]
    parsed = cli.build_parser().parse_args(argv)
    assert parsed.detach is False and parsed.run_id is None  # off unless asked for
    assert cli.main(argv) == 0
    assert det.spawned == []  # the attached path never calls spawn_detached
    [call] = det.supervise_calls
    # The call an attached run made before the flag existed: no run id, nothing new.
    assert set(call) == {"cfg", "argv", "env", "home", "incident_id", "resumed_from"}
    assert call["argv"] == BUILD and call["env"] is os.environ
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_the_detached_supervisor_runs_under_the_run_id_it_was_given(
    workspace: Path, det: Detached
) -> None:
    run_id = "run-20270101000000-abcdef"
    argv = ["run", "--workspace", str(workspace), "--run-id", run_id, "--", *BUILD]
    assert cli.main(argv) == 0
    assert det.supervise_calls[0]["run_id"] == run_id and det.spawned == []


def test_a_run_id_that_is_not_one_is_refused(
    workspace: Path, det: Detached, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "--workspace", str(workspace), "--run-id", "../x", "--", *BUILD]
    assert cli.main(argv) == 2
    assert "is not a run id" in capsys.readouterr().err
    assert det.supervise_calls == [] and det.spawned == []


def test_supervise_uses_the_given_run_id_once(tmp_path: Path, recovery_home: Path) -> None:
    cfg = WorkspaceConfig.load(_toml_workspace(tmp_path, "print('hi')\n"))
    store = Store.at_home(recovery_home)
    try:
        run = runner.supervise(
            cfg,
            store,
            ["status"],
            env=dict(os.environ),
            home=recovery_home,
            echo=False,
            run_id="run-20270101000000-abcdef",
            awake=lambda _flags: None,
        )
        assert run.run_id == "run-20270101000000-abcdef" and run.exit_code == 0
        assert run.stdout_path == str(recovery_home / "runs" / run.run_id / "stdout.log")
        with pytest.raises(ValueError, match="already recorded"):
            runner.supervise(
                cfg, store, ["status"], env=dict(os.environ), home=recovery_home, run_id=run.run_id
            )
        assert store.lease_holder(cfg.lease_name) is None
        assert [r.run_id for r in store.runs()] == [run.run_id]
    finally:
        store.close()


# wait


def test_wait_returns_the_runs_exit_code_and_stdout_tail(
    workspace: Path,
    recovery_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    me = procs.self_key()
    old = _record("run-20270101000000-000001", workspace, recovery_home, started_at=10.0)
    old.status, old.exit_code = RunStatus.EXITED, 0
    _stdout(old, "the run before\n")
    run = _record(
        "run-20270101000000-000002",
        workspace,
        recovery_home,
        started_at=20.0,
        supervisor_pid=me.pid,
        supervisor_created=me.created,
    )
    _stdout(run, "".join(f"line {n}\n" for n in range(1, 6)))
    _save(recovery_home, old, run)
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        """The run ends, as its supervisor would record it, during the third poll wait."""
        polls.append(seconds)
        if len(polls) == 3:
            with Path(run.stdout_path).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("build thread build-p-m1 phase=halted exit=3\n")
            run.status, run.exit_code, run.ended_at = RunStatus.EXITED, 3, 30.0
            run.supervisor_pid, run.supervisor_created = 0, 0.0  # its supervisor is gone
            _save(recovery_home, run)

    monkeypatch.setattr(cli.time, "sleep", sleep)
    argv = ["wait", "--workspace", str(workspace), "--tail", "2", "--interval", "7"]
    assert cli.main(argv) == 3
    assert polls == [7.0, 7.0, 7.0]  # it waited while the run ran, and stopped when it ended
    out = capsys.readouterr()
    assert out.out.splitlines() == ["line 5", "build thread build-p-m1 phase=halted exit=3"]
    assert f"recovery wait: run {run.run_id} exited, exit 3" in out.err

    # Started again, after the end: the same answer, at once.
    polls.clear()
    assert cli.main(argv) == 3 and polls == []
    assert capsys.readouterr().out.splitlines()[-1].endswith("exit=3")
    # The named run: the one before.
    assert cli.main([*argv, "--run", old.run_id]) == 0
    assert capsys.readouterr().out.splitlines() == ["the run before"]


def test_wait_exit_codes_for_a_refused_a_killed_and_an_orphaned_run(
    workspace: Path, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = _record("run-20270101000000-00000a", workspace, recovery_home, started_at=30.0)
    refused.status = RunStatus.REFUSED
    killed = _record("run-20270101000000-00000b", workspace, recovery_home, started_at=20.0)
    killed.status = RunStatus.KILLED_HANG
    orphan = _record(  # still "running" in the store; both of its processes are gone
        "run-20270101000000-00000c",
        workspace,
        recovery_home,
        started_at=10.0,
        pid=999_999,
        pid_created=1.0,
        supervisor_pid=999_998,
        supervisor_created=1.0,
    )
    _save(recovery_home, refused, killed, orphan)
    base = ["wait", "--workspace", str(workspace), "--interval", "0.01"]
    assert cli.main([*base, "--run", refused.run_id]) == 2
    assert cli.main([*base, "--run", killed.run_id]) == 1
    assert "killed_hang, exit -" in capsys.readouterr().err
    assert cli.main([*base, "--run", orphan.run_id]) == 1
    assert "its supervisor and its build are gone" in capsys.readouterr().err
    # Without --run: the newest run that was started, never the refused attempt after it.
    assert cli.main(base) == 1
    assert killed.run_id in capsys.readouterr().err


def test_wait_finds_the_replay_as_the_newest_run_and_prints_its_incident(
    workspace: Path, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _record("run-20270101000000-00000d", workspace, recovery_home, started_at=10.0)
    first.status, first.exit_code = RunStatus.EXITED, 1
    replay = _record(
        "run-20270101000000-00000e",
        workspace,
        recovery_home,
        started_at=20.0,
        incident_id="inc-9",
        resumed_from=first.run_id,
    )
    replay.status, replay.exit_code = RunStatus.EXITED, 1
    _stdout(replay, "replayed\n")
    incident = Incident(
        incident_id="inc-9",
        signature="abc",
        failure_class=FailureClass.HARNESS_SUSPECT,
        summary="TimeoutExpired in run",
        workspace=str(workspace.resolve()),
        state=IncidentState.DETECTED,
        created_at=1.0,
        updated_at=1.0,
    )
    store = Store.at_home(recovery_home)
    try:
        store.save_run(first)
        store.save_run(replay)
        store.save_incident(incident)
    finally:
        store.close()
    assert cli.main(["wait", "--workspace", str(workspace)]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines() == ["replayed"]
    assert (
        "recovery: incident inc-9 (harness_suspect) opened; recovery continues in the "
        "background. python -m recovery status"
    ) in out.err.splitlines()


def _dump(home: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(str(home / "state.sqlite"))
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_wait_takes_no_lease_and_writes_nothing(
    workspace: Path,
    recovery_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    me = procs.self_key()
    holder = ProcessKey(me.pid, me.created)
    run = _record(
        "run-20270101000000-00000f",
        workspace,
        recovery_home,
        supervisor_pid=me.pid,
        supervisor_created=me.created,
    )
    _stdout(run, "working\n")
    cfg = WorkspaceConfig.load(workspace)
    store = Store.at_home(recovery_home)
    try:
        store.save_run(run)
        assert store.acquire_lease(cfg.lease_name, holder, procs.alive, note=run.run_id)
    finally:
        store.close()
    before = _dump(recovery_home)
    writes: list[str] = []
    for name in ("save_run", "save_incident", "add_event", "acquire_lease", "release_lease"):
        monkeypatch.setattr(
            Store, name, lambda self, *a, _name=name, **k: writes.append(_name), raising=True
        )
    monkeypatch.setattr(Store, "step", lambda self, *a, **k: writes.append("step"))

    def killed(_seconds: float) -> None:
        """The operator kills ``wait`` while the run is still running."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", killed)
    assert cli.main(["wait", "--workspace", str(workspace)]) == 130
    assert "is not affected" in capsys.readouterr().err
    assert writes == []
    assert _dump(recovery_home) == before  # the run, and its supervisor's lease, as they were


def test_wait_on_a_missing_store_or_an_unknown_run_exits_2(
    workspace: Path, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["wait", "--workspace", str(workspace)]) == 2
    assert capsys.readouterr().err.splitlines() == [
        f"recovery wait: no supervisor store in {recovery_home}"
    ]
    assert not recovery_home.exists()  # waiting never creates a store
    other = workspace.parent / "other"
    other.mkdir()
    _save(recovery_home, _record("run-20270101000000-000010", other, recovery_home))
    assert cli.main(["wait", "--workspace", str(workspace)]) == 2
    assert "no supervised run of" in capsys.readouterr().err
    assert cli.main(["wait", "--workspace", str(workspace), "--run", "run-nope"]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "recovery wait: no run run-nope (python -m recovery status)"
    ]
    argv = ["wait", "--workspace", str(workspace), "--run", "run-20270101000000-000010"]
    assert cli.main(argv) == 2
    assert "is a run of" in capsys.readouterr().err


# One real detached run, no model


LAUNCHER = """\
import sys, time
from pathlib import Path

go = Path(sys.argv[0]).with_name("go")
print("build started", flush=True)
deadline = time.time() + 120
while not go.exists() and time.time() < deadline:
    time.sleep(0.05)
print("build thread build-p-m1 phase=done exit=None")
"""


def _toml_workspace(tmp_path: Path, launcher_text: str) -> Path:
    """A workspace whose launcher is a tiny Python script: it calls no model and no
    GitHub, and exits 0, which the supervisor classifies as done (no incident, no tick)."""
    ws = tmp_path / "real-ws"
    ws.mkdir()
    launcher = ws / "launcher.py"
    launcher.write_text(launcher_text, encoding="utf-8", newline="\n")
    lines = [
        f"launcher = {json.dumps([sys.executable, str(launcher)])}",
        f"scio_repo = {json.dumps((tmp_path / 'no-scio-repo').as_posix())}",
        'scio_gh_repo = "nobody/nothing"',
        "[limits]",
        "poll_s = 0.1",
    ]
    (ws / "recovery.toml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return ws


def _until(done: Any, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_a_detached_run_outlives_its_starter_and_wait_reports_it(
    tmp_path: Path, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = _toml_workspace(tmp_path, LAUNCHER)
    env = {**os.environ, "SCIO_RECOVERY_HOME": str(recovery_home)}
    detach = [sys.executable, "-m", "recovery", "run", "--detach"]
    try:
        starter = subprocess.run(
            [*detach, "--workspace", str(ws), "--", "status"],
            cwd=cli.PACKAGE_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        # The starter has ended (subprocess.run returned), and it named the run.
        assert starter.returncode == 0, starter.stderr
        [run] = _runs(recovery_home)
        assert starter.stdout.splitlines() == [
            f"recovery run: detached run {run.run_id} (pid {run.supervisor_pid}); "
            f"stdout: {run.stdout_path}"
        ]
        # The run goes on without it, under a supervisor that is not this process's child.
        _until(lambda: "build started" in _read(run.stdout_path))
        [run] = _runs(recovery_home)
        assert run.status is RunStatus.RUNNING
        assert procs.alive(ProcessKey(run.supervisor_pid, run.supervisor_created))
        assert procs.alive(run.process)
        # A second start on the busy workspace is refused, and starts nothing.
        again = subprocess.run(
            [*detach, "--workspace", str(ws), "--", "status"],
            cwd=cli.PACKAGE_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert again.returncode == 2 and "is busy" in again.stderr
        assert [r.run_id for r in _runs(recovery_home)] == [run.run_id]
    finally:
        (ws / "go").write_text("", encoding="utf-8")  # let the build end, whatever happened
    assert cli.main(["wait", "--workspace", str(ws), "--interval", "0.1"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["build started", "build thread build-p-m1 phase=done exit=None"]
    assert f"recovery wait: run {run.run_id} exited, exit 0" in out.err
    [ended] = _runs(recovery_home)
    assert ended.status is RunStatus.EXITED and ended.exit_code == 0
    assert ended.incident_id is None
    # The supervisor is gone, and it released the lease it held.
    assert not procs.alive(ProcessKey(run.supervisor_pid, run.supervisor_created))
    store = Store.at_home(recovery_home)
    try:
        _until(lambda: store.lease_holder(WorkspaceConfig.load(ws).lease_name) is None, 10.0)
        assert store.open_incidents() == []
    finally:
        store.close()
    assert (recovery_home / "runs" / run.run_id / "supervisor.log").is_file()


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
