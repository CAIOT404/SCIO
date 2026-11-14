"""A used-up usage window is waited out, never repaired (issue #126).

At 21:46Z on 2026-11-14 graphcalc's P27 attempt 1 failed with ``claude -p failed: You've hit
your 5-hour limit · resets 2:20pm (America/Los_Angeles)``. No transient marker matched that
wording, so the run was ``harness_suspect``: two repair sessions started, hit the same
limit, and the incident escalated. The window is ``external_transient`` now, and the
backoff lasts until the reset time the message names, plus a margin."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest

from recovery import classify as classify_module
from recovery import evidence, machine
from recovery.classify import classify
from recovery.commands import CommandResult
from recovery.config import Limits, WorkspaceConfig
from recovery.machine import Deps
from recovery.model import (
    Evidence,
    FailureClass,
    Incident,
    IncidentState,
    RunRecord,
    RunStatus,
)
from recovery.store import Store

FAILED_AT = datetime(2026, 11, 14, 21, 46, tzinfo=UTC).timestamp()
LIMIT = "claude -p failed: You've hit your 5-hour limit · resets 2:20pm (America/Los_Angeles)"
REASON_126 = (
    f"P27 attempt 1 failed: FatalError: {LIMIT} (subtype=success); the next `scio build` "
    "starts attempt 2"
)
"""The outcome file's ``reason`` of ``run-20261114213150-e88e7c``, as SCIO wrote it."""

PACIFIC_STANDARD = timezone(timedelta(hours=-8))
"""America/Los_Angeles in November, standing in for this machine's local time."""


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


# The class


@pytest.mark.parametrize(
    ("halt_reason", "line"),
    [
        ("feature_failed", REASON_126),  # #126's ledger line, from the outcome file
        # the stdout fallback: SCIO's cp1252 output turns the middle dot into U+FFFD
        ("feature_failed", REASON_126.replace("·", "�")),
        # a typographic apostrophe and the weekly window
        (
            "feature_failed",
            "P3 attempt 2 failed: FatalError: claude -p failed: You"
            + chr(0x2019)
            + "ve hit your weekly limit · resets 9am (Europe/Berlin) (subtype=success)",
        ),
        # the same limit in a harness error no node caught (I-182's stop line)
        (
            "harness",
            f"scio build stopped on a harness error: FatalError: {LIMIT} (subtype=success)",
        ),
    ],
)
def test_a_used_up_usage_window_is_transient_not_a_repair(halt_reason: str, line: str) -> None:
    found = classify(Evidence(exit_code=1, halt_reason=halt_reason, last_ledger_line=line))
    assert found.failure_class is FailureClass.EXTERNAL_TRANSIENT, found
    assert found.failure_class not in machine.REPAIRABLE


# The reset time


@pytest.mark.parametrize(
    ("text", "failed_at", "reset_at", "until"),
    [
        (LIMIT, FAILED_AT, _utc(2026, 11, 14, 22, 20), _utc(2026, 11, 14, 22, 25)),
        # minutes omitted
        ("resets 3pm (America/Los_Angeles)", FAILED_AT, _utc(2026, 11, 14, 23), None),
        ("Resets 2:20 p.m. (America/Los_Angeles)", FAILED_AT, _utc(2026, 11, 14, 22, 20), None),
        # summer time: 2:20pm is 21:20Z in July
        (LIMIT, _utc(2026, 7, 14, 20), _utc(2026, 7, 14, 21, 20), None),
        # a 24-hour time in UTC
        ("limit · resets 23:05 (UTC)", FAILED_AT, _utc(2026, 11, 14, 23, 5), None),
        # 9am Pacific has passed: the next one is tomorrow, 19 h away, so the wait is capped
        (
            "resets 9am (America/Los_Angeles)",
            FAILED_AT,
            _utc(2026, 11, 15, 17),
            FAILED_AT + 8 * 3600,
        ),
    ],
)
def test_the_reset_time_is_read_in_its_spellings(
    text: str, failed_at: float, reset_at: float, until: float | None
) -> None:
    reset = classify_module.usage_reset(text, failed_at)
    assert reset is not None
    assert reset.at == reset_at
    assert reset.until == (until if until is not None else reset_at + 300)
    assert reset.assumed == ""


def test_without_a_zone_database_the_named_time_is_read_as_this_machines_local_time() -> None:
    """The supervisor's scheduled tick runs a Windows Python with no time zone database
    (``TZPATH`` empty, no ``tzdata``), where every ``ZoneInfo`` fails. ``claude -p`` runs
    on the same machine and names its local zone, so the time is read as local time."""

    def no_database(key: str) -> tzinfo:
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    reset = classify_module.usage_reset(LIMIT, FAILED_AT, zone=no_database, local=PACIFIC_STANDARD)
    assert reset is not None
    assert (reset.at, reset.until) == (_utc(2026, 11, 14, 22, 20), _utc(2026, 11, 14, 22, 25))
    assert "no time zone database" in reset.assumed
    # a message that names no zone is read the same way
    unnamed = classify_module.usage_reset(
        "resets 2:20pm", FAILED_AT, zone=no_database, local=PACIFIC_STANDARD
    )
    assert unnamed is not None and unnamed.at == _utc(2026, 11, 14, 22, 20)
    assert "names no zone" in unnamed.assumed


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your 5-hour limit · resets soon",
        "resets 13pm (America/Los_Angeles)",
        "resets 2:75pm (America/Los_Angeles)",
        "resets 5 (America/Los_Angeles)",
        "resets 2:20pm (Mars/Olympus_Mons)",  # a zone the database does not know
        "rate limit, retry later",
    ],
)
def test_an_unreadable_reset_time_gives_none(text: str) -> None:
    def utc_only(key: str) -> tzinfo:
        if key == "UTC":
            return UTC
        raise zoneinfo.ZoneInfoNotFoundError(key)

    assert classify_module.usage_reset(text, FAILED_AT, zone=utc_only) is None


# The backoff


def _no_command(argv: Any, **_: Any) -> CommandResult:
    raise AssertionError(f"a test ran a real command: {argv}")


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> tuple[Incident, Deps, Clock]:
    """A build that halted on ``line`` at FAILED_AT, driven to its backoff 20 s later."""
    clock = Clock(FAILED_AT + 20)
    cfg = WorkspaceConfig(
        workspace=tmp_path / "ws",
        launcher=("sh", "scio.sh"),
        scio_repo=tmp_path / "SCIO",
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path / "SCIO" / ".scio" / "live-wt",
        limits=Limits(max_replays=3, backoff_s=(300.0, 900.0, 1800.0)),
    )
    ev = Evidence(exit_code=1, halt_reason="feature_failed", last_ledger_line=line)
    monkeypatch.setattr(evidence, "read_evidence", lambda run, **_: ev)
    monkeypatch.setattr(evidence, "snapshot", lambda run, cfg, dest, **_: {})

    def no_github(repo: str) -> Any:
        raise AssertionError("a transient failure opens no issue")

    def no_replay(run: RunRecord, incident_id: str) -> int:
        raise AssertionError("no replay before the backoff ends")

    deps = Deps(
        store=Store(":memory:", clock=clock),
        home=tmp_path / "home",
        package_root=tmp_path,
        config_for=lambda _ws: cfg,
        git=_no_command,
        uv=_no_command,
        claude=_no_command,
        launcher=_no_command,
        github_for=no_github,
        scio_running=list,
        start_replay=no_replay,
        clock=clock,
    )
    run = RunRecord(
        run_id="run-20261114213150-e88e7c",
        workspace=str(cfg.workspace),
        launcher=["sh", "scio.sh"],
        argv=["build", "--product", "P"],
        env={},
        started_at=FAILED_AT - 870,
        status=RunStatus.EXITED,
        exit_code=1,
        ended_at=FAILED_AT,
    )
    deps.store.save_run(run)
    incident = machine.on_run_finished(run, deps)
    assert incident is not None
    for _ in range(6):
        if incident.state is IncidentState.WAITING_BACKOFF or not incident.open:
            break
        incident = machine.advance(incident, deps)
    return incident, deps, clock


def test_the_backoff_waits_until_the_window_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident, deps, clock = _waiting(tmp_path, monkeypatch, REASON_126)
    assert incident.failure_class is FailureClass.EXTERNAL_TRANSIENT
    assert incident.state is IncidentState.WAITING_BACKOFF
    assert incident.backoff_until == _utc(2026, 11, 14, 22, 25)
    events = [text for _, text in deps.store.events(incident.incident_id)]
    assert any(
        "waiting for the usage window to reset at 2026-11-14 22:20Z" in text for text in events
    ), events
    clock.t = _utc(2026, 11, 14, 22, 24)
    assert machine.advance(incident, deps).state is IncidentState.WAITING_BACKOFF  # not yet


def test_an_unreadable_reset_time_falls_back_to_the_backoff_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = REASON_126.replace("resets 2:20pm (America/Los_Angeles)", "resets soon")
    incident, deps, clock = _waiting(tmp_path, monkeypatch, line)
    assert incident.failure_class is FailureClass.EXTERNAL_TRANSIENT
    assert incident.state is IncidentState.WAITING_BACKOFF
    assert incident.backoff_until == clock.t + 300.0  # backoff_s[0]
    events = [text for _, text in deps.store.events(incident.incident_id)]
    assert not any("usage window" in text for text in events), events
