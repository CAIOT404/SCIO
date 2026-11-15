"""`scripts/move_live_wt.py` (issue #122, W-27): live-wt moves to main only when no supervised
build runs and no incident is open, and recovery-wt never moves.

"Idle" is read from ``python -m recovery status``, run from ``.scio/recovery-wt``; the fake
runner answers it from a script of statuses, and its clock advances only by the tool's own
sleeps, so a four-hour wait takes no time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.session_tools_support import FakeRunner, clone, commit, git, load, make_hub, same

RUNNING = """runs (2, newest first):
  run-20261114-a1  running  exit -  build --product C:/graphcalc
    started 2026-11-14 10:00:00 UTC, ended -
  run-20261113-b2  exited  exit 0  build --product C:/graphcalc
    started 2026-11-13 09:00:00 UTC, ended 2026-11-13 11:00:00 UTC
open incidents: none
"""
INCIDENT = """runs (1, newest first):
  run-20261114-a1  exited  exit 1  build --product C:/graphcalc
    started 2026-11-14 10:00:00 UTC, ended 2026-11-14 10:30:00 UTC
    incident: inc-20261114103000-aa11bb
open incidents (newest first):
  inc-20261114103000-aa11bb  awaiting_merge  harness_fault  issue #130  PR #131
    a traceback in scio/loop/agent_loop.py
"""
IDLE = """runs (1, newest first):
  run-20261114-a1  exited  exit 0  build --product C:/graphcalc
    started 2026-11-14 10:00:00 UTC, ended 2026-11-14 12:00:00 UTC
open incidents: none
escalated, waiting for a human (newest first):
  inc-20261101000000-cc22dd  escalated  harness_fault  issue #90  PR -
    an old one; escalated incidents are closed for this purpose
"""


def _pinned(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """live-wt and recovery-wt pinned at ``old``; main has since moved to ``new``."""
    hub = make_hub(tmp_path)
    scio = clone(hub, tmp_path / "SCIO")
    old = git(scio, "rev-parse", "HEAD")
    live, recovery = scio / ".scio" / "live-wt", scio / ".scio" / "recovery-wt"
    git(scio, "worktree", "add", "-q", "--detach", str(live), old)
    git(scio, "worktree", "add", "-q", "--detach", str(recovery), old)
    new = commit(hub, {"scio_runtime.py": "fixed = True\n"}, "a merged repair")
    return scio, live, recovery, old, new


def _statuses(*outputs: tuple[int, str]) -> tuple[FakeRunner, list[Path | None]]:
    queue = list(outputs)
    cwds: list[Path | None] = []

    def python(argv: list[str], cwd: Path | None) -> tuple[int, str]:
        assert argv == ["-m", "recovery", "status"], argv
        cwds.append(cwd)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return FakeRunner(python=python), cwds


def test_live_wt_moves_only_when_no_supervised_build_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mv = load("move_live_wt")
    scio, live, recovery, old, new = _pinned(tmp_path)

    # A build runs, then an incident is open, then the supervisor is idle: it moves then.
    runner, cwds = _statuses((0, RUNNING), (0, INCIDENT), (0, IDLE))
    assert mv.main(["--when-idle", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    assert runner.sleeps == [60.0, 60.0]
    assert "supervised run: run-20261114-a1  running" in out
    assert "open incident: inc-20261114103000-aa11bb" in out
    assert f"live-wt moved: {old[:7]} -> {new[:7]} (origin/main)" in out
    assert git(live, "rev-parse", "HEAD") == new
    assert git(recovery, "rev-parse", "HEAD") == old, "recovery-wt moves only on the owner's word"
    assert cwds and all(same(c, recovery) for c in cwds if c is not None)

    # Moved already: nothing to do.
    runner, _ = _statuses((0, IDLE))
    assert mv.main(["--root", str(scio)], runner=runner) == 0
    assert f"already at origin/main {new[:7]}" in capsys.readouterr().out


def test_live_wt_waits_no_longer_than_max_wait_and_says_on_what(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mv = load("move_live_wt")
    scio, live, _recovery, old, _new = _pinned(tmp_path)

    runner, _ = _statuses((0, RUNNING))
    assert mv.main(["--when-idle", "--max-wait", "120", "--root", str(scio)], runner) == 3
    out = capsys.readouterr().out
    assert runner.sleeps == [60.0, 60.0]
    assert "live-wt not moved; waited on:" in out and "run-20261114-a1  running" in out
    assert git(live, "rev-parse", "HEAD") == old

    # A status that cannot be read is never taken as idle.
    runner, _ = _statuses((1, "Traceback (most recent call last): ..."))
    assert mv.main(["--root", str(scio)], runner) == 3
    assert "could not be read" in capsys.readouterr().out
    assert git(live, "rev-parse", "HEAD") == old
