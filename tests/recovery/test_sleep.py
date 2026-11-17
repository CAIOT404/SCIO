"""A machine sleep is not a hang (I-240, issue #158): the supervisor's poll loop does not
count the time the machine was down as idle time, still kills a real hang, and asks
Windows to stay awake while its child is alive. A fake child and an injected clock: no
test here sleeps or starts a process."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from recovery import cli, runner
from recovery.commands import CommandResult
from recovery.config import Limits, WorkspaceConfig
from recovery.model import RunRecord, RunStatus
from recovery.store import Store

HOUR_S = 3600.0
POLL_S = 15.0
HANG_S = 2700.0


class Clock:
    """Time moves only when the supervisor sleeps. ``jumps`` maps the number of a sleep
    (1 is the first) to the extra seconds the machine was down during it."""

    def __init__(self, jumps: dict[int, float] | None = None) -> None:
        self.start = time.time()
        self.now = self.start
        self.jumps = jumps or {}
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds + self.jumps.get(self.sleeps, 0.0)


class Child:
    """A child that exits 0 at its ``exits_at_poll``-th poll, or never (None)."""

    def __init__(self, exits_at_poll: int | None) -> None:
        self.pid = os.getpid()
        self.exits_at_poll = exits_at_poll
        self.polls = 0
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.killed:
            return 1
        if self.exits_at_poll is not None and self.polls >= self.exits_at_poll:
            return 0
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 1 if self.killed else 0

    def kill(self) -> None:
        self.killed = True


def _git(*_a: Any, **_kw: Any) -> CommandResult:
    return CommandResult(0, "abc123\n", "")


def _cfg(tmp_path: Path) -> WorkspaceConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    repo = tmp_path / "SCIO"
    return WorkspaceConfig(
        workspace=ws,
        launcher=("scio",),
        scio_repo=repo,
        scio_gh_repo="o/SCIO",
        live_wt=repo / ".scio" / "live-wt",
        limits=Limits(poll_s=POLL_S, hang_after_s=HANG_S),
    )


def _supervise(
    tmp_path: Path, child: Child, clock: Clock, **kw: Any
) -> tuple[RunRecord, Store, list[float]]:
    """Run ``supervise`` on the fake child. The list holds the clock at each kill."""
    store = Store(tmp_path / "s.sqlite")
    kills: list[float] = []

    def kill(pid: int) -> None:
        kills.append(clock.now)
        child.kill()

    def spawn(*_a: Any, **_kw: Any) -> subprocess.Popen[bytes]:
        return child  # type: ignore[return-value]

    kw.setdefault("spawn", spawn)
    kw.setdefault("awake", lambda flags: None)
    run = runner.supervise(
        _cfg(tmp_path),
        store,
        ["build", "--product", "P"],
        env={},
        home=tmp_path / "home",
        echo=False,
        clock=clock,
        sleep=clock.sleep,
        kill=kill,
        git=_git,
        **kw,
    )
    return run, store, kills


def test_a_sleep_gap_is_not_counted_as_a_hang(tmp_path: Path) -> None:
    # The machine sleeps 29 h during the supervisor's second wait (run-20261116005554).
    clock = Clock(jumps={2: 29 * HOUR_S})
    run, store, kills = _supervise(tmp_path, Child(exits_at_poll=6), clock)
    assert kills == []
    assert run.status is RunStatus.EXITED
    assert run.exit_code == 0
    assert run.downtime_s == pytest.approx(29 * HOUR_S + POLL_S)
    assert run.notes == ["machine was down 29.0 h; not counted as idle"]
    saved = store.get_run(run.run_id)
    assert saved is not None
    assert saved.downtime_s == pytest.approx(29 * HOUR_S + POLL_S)
    assert saved.notes == run.notes


def test_a_real_hang_with_the_loop_running_is_still_killed(tmp_path: Path) -> None:
    clock = Clock()
    run, _store, kills = _supervise(tmp_path, Child(exits_at_poll=None), clock)
    assert run.status is RunStatus.KILLED_HANG
    assert len(kills) == 1
    assert HANG_S < kills[0] - clock.start <= HANG_S + 2 * POLL_S
    assert run.downtime_s == 0.0
    assert run.notes == []


def test_a_hang_after_a_sleep_is_killed_after_hang_after_s_of_awake_time(tmp_path: Path) -> None:
    # The downtime is excused once. It does not buy a hung child 29 more hours.
    clock = Clock(jumps={2: 29 * HOUR_S})
    run, _store, kills = _supervise(tmp_path, Child(exits_at_poll=None), clock)
    assert run.status is RunStatus.KILLED_HANG
    awake = kills[0] - clock.start - run.downtime_s
    assert HANG_S < awake <= HANG_S + 2 * POLL_S


def test_two_sleeps_are_both_excused_and_each_is_noted(tmp_path: Path) -> None:
    clock = Clock(jumps={2: 2 * HOUR_S, 4: 3 * HOUR_S})
    run, _store, kills = _supervise(tmp_path, Child(exits_at_poll=8), clock)
    assert kills == []
    assert run.downtime_s == pytest.approx(5 * HOUR_S + 2 * POLL_S)
    assert len(run.notes) == 2


def test_a_slow_poll_below_the_sleep_gap_counts_as_idle_time(tmp_path: Path) -> None:
    # Four minutes between two polls is a slow machine, not a sleeping one.
    clock = Clock(jumps={2: 240.0})
    run, _store, _kills = _supervise(tmp_path, Child(exits_at_poll=6), clock)
    assert run.downtime_s == 0.0
    assert run.notes == []


def test_the_sleep_gap_is_never_within_reach_of_a_normal_poll() -> None:
    assert runner.sleep_gap_s(15.0) == runner.SLEEP_GAP_S >= 300.0
    assert runner.sleep_gap_s(600.0) >= 4 * 600.0


def test_idle_time_excludes_only_the_downtime_after_the_last_progress() -> None:
    # Progress at 100; down 200 -> 1000; now 1100: awake and idle for 100 + 100 s.
    assert runner.idle_s(1100.0, 100.0, [(200.0, 1000.0)]) == pytest.approx(200.0)
    # Progress written at 250, during the gap's first poll wait: only 250 -> 1000 was down.
    assert runner.idle_s(1100.0, 250.0, [(200.0, 1000.0)]) == pytest.approx(100.0)
    # Progress after the gap: the old downtime excuses nothing.
    assert runner.idle_s(5000.0, 1500.0, [(200.0, 1000.0)]) == pytest.approx(3500.0)
    assert runner.idle_s(50.0, 10.0, []) == pytest.approx(40.0)


# Stay awake


def test_a_supervised_run_asks_windows_to_stay_awake(tmp_path: Path) -> None:
    child = Child(exits_at_poll=3)
    events: list[Any] = []

    def spawn(*_a: Any, **_kw: Any) -> Any:
        events.append("spawn")
        return child

    def awake(flags: int) -> None:
        events.append((flags, child.polls))

    _supervise(tmp_path, child, Clock(), spawn=spawn, awake=awake)
    stay = runner.ES_CONTINUOUS | runner.ES_SYSTEM_REQUIRED
    # Set before the child starts, released (ES_CONTINUOUS alone) after its last poll.
    assert events == [(stay, 0), "spawn", (runner.ES_CONTINUOUS, 3)]
    assert (runner.ES_CONTINUOUS, runner.ES_SYSTEM_REQUIRED) == (0x80000000, 0x00000001)


def test_the_stay_awake_request_is_released_when_the_child_raises(tmp_path: Path) -> None:
    events: list[Any] = []

    def spawn(*_a: Any, **_kw: Any) -> Any:
        events.append("spawn")
        raise RuntimeError("the child blew up")

    with pytest.raises(RuntimeError, match="blew up"):
        _supervise(tmp_path, Child(exits_at_poll=1), Clock(), spawn=spawn, awake=events.append)
    stay = runner.ES_CONTINUOUS | runner.ES_SYSTEM_REQUIRED
    assert events == [stay, "spawn", runner.ES_CONTINUOUS]


def test_a_refused_stay_awake_request_never_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[int] = []

    def awake(flags: int) -> None:
        calls.append(flags)
        raise OSError("SetThreadExecutionState returned NULL")

    run, _store, _kills = _supervise(tmp_path, Child(exits_at_poll=3), Clock(), awake=awake)
    assert run.status is RunStatus.EXITED
    assert run.exit_code == 0
    # Nothing was set, so nothing is released.
    assert calls == [runner.ES_CONTINUOUS | runner.ES_SYSTEM_REQUIRED]
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "stay awake" in err[0]


def test_the_execution_state_call_is_a_no_op_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.sys, "platform", "linux")
    runner.set_execution_state(runner.ES_CONTINUOUS | runner.ES_SYSTEM_REQUIRED)


# The record and its display


def _old_record() -> dict[str, Any]:
    """A run record as v0.10.0 stored it: no ``downtime_s``, no ``notes``."""
    return {
        "run_id": "run-20261116005554-052972",
        "workspace": "W",
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P"],
        "env": {},
        "started_at": 100.0,
        "status": "killed_hang",
        "last_progress_at": 0.0,
        "exit_code": 1,
    }


def test_a_run_record_without_downtime_still_loads(tmp_path: Path) -> None:
    run = RunRecord.from_dict(_old_record())
    assert run.status is RunStatus.KILLED_HANG
    assert run.downtime_s == 0.0
    assert run.notes == []
    store = Store(tmp_path / "s.sqlite")
    store.save_run(run)
    again = store.get_run(run.run_id)
    assert again is not None
    assert again.downtime_s == 0.0
    assert again.notes == []


def test_status_shows_a_runs_downtime_only_when_it_has_some() -> None:
    run = RunRecord.from_dict(_old_record())
    assert not [ln for ln in cli._run_lines(run) if "downtime" in ln]
    run.downtime_s = 29 * HOUR_S
    assert "    downtime: 29.0 h (not counted as idle)" in cli._run_lines(run)
