"""`scripts/metrics.py` and `scripts/slip.py` (issue #141, parts 2 and 3a): the bottleneck audit
as numbers, and slips counted in a machine log instead of the ledger.

The owner's words (#141): "Measure, using the industry-standard DORA metrics, adapted. For SCIO
PRs: lead time (open to merge), gate and CI time, and the share of PRs that need rework. For
graphcalc: features per day, cost per feature, the share of attempts that escalate, and time
stalled waiting on you." And: "Slips caught within the same turn with no effect become counts,
not full ledger entries."

GitHub is faked through the session tools' runner (no test calls a live ``gh``); the machine
directory and the recovery supervisor's home are this test's own directories, so nothing here
reads or writes the real ``%LOCALAPPDATA%/SCIO``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.session_tools_support import (
    SCRIPTS,
    FakeRunner,
    clone,
    commit,
    git,
    load,
    make_hub,
    same,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
"""Seven days back from here is 2026-11-08 12:00 UTC."""


@pytest.fixture(autouse=True)
def machine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The machine directory and the supervisor's home are this test's own."""
    machine = tmp_path / "machine"
    monkeypatch.setenv("LOCALAPPDATA", str(machine))
    monkeypatch.setenv("XDG_CACHE_HOME", str(machine))
    monkeypatch.setenv("SCIO_RECOVERY_HOME", str(tmp_path / "recovery"))
    return machine / ("SCIO" if os.name == "nt" else "scio")


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(
    number: int, opened: datetime, merged: datetime, branch: str, labels: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A merged PR as ``gh pr list --json`` prints it. ``updatedAt`` and ``closedAt`` differ
    from ``mergedAt`` on purpose: a lead time read from either is wrong."""
    return {
        "number": number,
        "title": f"PR {number}",
        "createdAt": _iso(opened),
        "mergedAt": _iso(merged),
        "closedAt": _iso(merged + timedelta(minutes=1)),
        "updatedAt": _iso(merged + timedelta(hours=30)),
        "headRefName": branch,
        "labels": [{"id": "x", "name": name, "description": "", "color": "fff"} for name in labels],
    }


def _run(
    branch: str,
    created: datetime,
    conclusion: str,
    *,
    event: str = "pull_request",
    minutes: float = 5.0,
    attempt: int = 1,
) -> dict[str, Any]:
    """A CI run as ``gh run list --json`` prints it."""
    return {
        "databaseId": abs(hash((branch, created, conclusion))) % 10**9,
        "headBranch": branch,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "attempt": attempt,
        "createdAt": _iso(created),
        "startedAt": _iso(created),
        "updatedAt": _iso(created + timedelta(minutes=minutes)),
    }


D = timedelta(days=1)
H = timedelta(hours=1)
PRS = [
    _pr(201, NOW - 2 * D, NOW - 2 * D + 1 * H, "feat/201-x"),
    # Opened before the window, merged in it: 144 h.
    _pr(202, NOW - 9 * D, NOW - 3 * D, "docs/202-y", ("low-risk", "documentation")),
    _pr(203, NOW - 1 * D, NOW - 1 * D + 3 * H, "fix/203-z"),
    # Merged on the window's first day but before its first hour: the search is by date
    # (`merged:>=2026-11-08`), so GitHub returns it, and the window leaves it out.
    _pr(150, NOW - 10 * D, NOW - 7 * D - 2 * H, "feat/150-old"),
]
RUNS = [
    # 201: a failed run, then a green one: rework.
    _run("feat/201-x", NOW - 2 * D + 5 * timedelta(minutes=1), "failure", minutes=4),
    _run("feat/201-x", NOW - 2 * D + 30 * timedelta(minutes=1), "success", minutes=6),
    # 202: green; the failed run on its branch name is older than the PR (a reused name).
    _run("docs/202-y", NOW - 9 * D - 2 * H, "failure", minutes=3),
    _run("docs/202-y", NOW - 9 * D + 1 * H, "success", minutes=9),
    _run("docs/202-y", NOW - 4 * D, "success", minutes=2),
    # 203: a cancelled run is not a failure; its green run was re-run once (attempt 2).
    _run("fix/203-z", NOW - 1 * D + 10 * timedelta(minutes=1), "cancelled", minutes=1),
    _run("fix/203-z", NOW - 1 * D + 1 * H, "success", minutes=8, attempt=2),
    # main's push run failed while 203 was open: main's, not 203's.
    _run("main", NOW - 1 * D + 2 * H, "failure", event="push", minutes=7),
]


def _gh(prs: list[dict[str, Any]], runs: list[dict[str, Any]], seen: list[list[str]]) -> Any:
    def gh(args: list[str]) -> tuple[int, str]:
        seen.append(args)
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(prs)
        if args[:2] == ["run", "list"]:
            return 0, json.dumps(runs)
        return 1, f"unexpected gh {args}"

    return gh


def _metrics(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    runner: FakeRunner,
    now: datetime = NOW,
) -> dict[str, Any]:
    metrics = load("metrics")
    capsys.readouterr()
    assert metrics.main([*argv, "--json"], runner=runner, now=now) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, dict)
    return data


def test_lead_time_is_measured_from_open_to_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 1, "lead time (open to merge)": ``createdAt`` to ``mergedAt`` of each PR
    merged in the window, as median and p90 (nearest rank), and the share labelled
    ``low-risk``. A PR the date search returns but that merged before the window is out."""
    seen: list[list[str]] = []
    data = _metrics(capsys, ["--root", str(tmp_path)], FakeRunner(gh=_gh(PRS, RUNS, seen)))
    prs = data["prs"]
    assert prs["merged"] == 3
    assert prs["lead_time_hours"] == {"n": 3, "median": 3.0, "p90": 144.0}
    assert prs["low_risk"] == 1 and prs["low_risk_share"] == pytest.approx(1 / 3)
    assert "createdAt" in prs["source"] and "mergedAt" in prs["source"]
    [pr_list] = [a for a in seen if a[:2] == ["pr", "list"]]
    assert pr_list[pr_list.index("--state") + 1] == "merged"
    assert pr_list[pr_list.index("--search") + 1] == "merged:>=2026-11-08"
    assert int(pr_list[pr_list.index("--limit") + 1]) >= 500
    assert data["window"] == {
        "days": 7.0,
        "since": "2026-11-08T12:00:00Z",
        "until": "2026-11-15T12:00:00Z",
    }


def test_rework_counts_a_pr_with_a_failed_ci_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 2, "the share of PRs that need rework": a PR with at least one failed CI run
    (``failure``, ``timed_out``, ``startup_failure``) on its head branch, from a
    ``pull_request`` event between its opening and its merge. A cancelled run, a failed run
    of the same branch name before the PR opened, and main's failed push run do not count. A
    re-run (``attempt`` above 1) hides its first verdict, so it is reported apart."""
    seen: list[list[str]] = []
    data = _metrics(capsys, ["--root", str(tmp_path)], FakeRunner(gh=_gh(PRS, RUNS, seen)))
    prs = data["prs"]
    assert prs["rework"] == 1 and prs["rework_prs"] == [201]
    assert prs["rework_share"] == pytest.approx(1 / 3)
    assert prs["reruns"] == 1 and prs["rerun_prs"] == [203]
    [run_list] = [a for a in seen if a[:2] == ["run", "list"]]
    assert run_list[run_list.index("--workflow") + 1] == "ci.yml"
    # The runs reach back to the opening of the oldest PR merged in the window (#202,
    # 2026-11-06), not only to the window's start, so its runs before the window count.
    assert run_list[run_list.index("--created") + 1] == ">=2026-11-06"
    assert int(run_list[run_list.index("--limit") + 1]) >= 1000


def test_ci_time_is_the_completed_runs_in_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI half of "gate and CI time": every completed run of the CI workflow created in the
    window, ``startedAt`` to ``updatedAt``; a run still going and one before the window are
    out. A result as long as the limit says it may be cut."""
    runs = [
        *RUNS,
        _run("feat/150-old", NOW - 8 * D, "success", minutes=50),
        {**_run("feat/new", NOW - 1 * H, "success", minutes=1), "status": "in_progress"},
    ]
    data = _metrics(capsys, ["--root", str(tmp_path)], FakeRunner(gh=_gh(PRS, runs, [])))
    ci = data["ci"]
    # In the window: 4, 6, 2, 1, 8 and 7 minutes (202's two runs before it are out).
    assert ci["minutes"] == {"n": 6, "median": 5.0, "p90": 8.0}
    assert ci["truncated"] is False
    assert "startedAt" in ci["source"] and "updatedAt" in ci["source"]


def _gate_line(log: Path, at: datetime, mode: str, minutes: float, passed: bool) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": _iso(at), "mode": mode, "minutes": minutes, "passed": passed}
    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({**entry, "tree": "t", "worktree": "w"}) + "\n")


def test_gate_time_reads_the_machine_gate_log(
    tmp_path: Path, machine_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate half of "gate and CI time": ``gate-runs.log`` in the machine directory, which
    ``scripts/gate.py`` appends to after every finished gate. A line before the window, and a
    line that is not JSON, are left out."""
    log = machine_dir / "gate-runs.log"
    _gate_line(log, NOW - 8 * D, "full", 40.0, True)
    _gate_line(log, NOW - 3 * D, "quick", 2.0, True)
    _gate_line(log, NOW - 2 * D, "quick", 4.0, False)
    _gate_line(log, NOW - 1 * D, "full", 12.0, True)
    with log.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    data = _metrics(capsys, ["--root", str(tmp_path)], FakeRunner(gh=_gh(PRS, RUNS, [])))
    gate = data["gate"]
    assert gate["minutes"] == {"n": 3, "median": 4.0, "p90": 12.0}
    assert gate["passed"] == 2 and gate["pass_rate"] == pytest.approx(2 / 3)
    assert gate["by_mode"] == {"full": 1, "quick": 2}


def _store_run(
    store: Any,
    tmp: Path,
    run_id: str,
    workspace: Path,
    started: datetime,
    ended: datetime | None,
    outcome: dict[str, Any] | None,
    exit_code: int | None,
) -> None:
    from recovery.model import RunRecord, RunStatus

    run_dir = tmp / "recovery" / "runs" / run_id
    run_dir.mkdir(parents=True)
    for name in ("stdout.log", "stderr.log"):
        (run_dir / name).write_text("", encoding="utf-8")
    if outcome is not None:
        base = {"version": 1, "command": "build", "thread": "build-graphcalc-m4"}
        empty = {"exit_reason": None, "reason": None, "resume": None, "exception": None}
        (run_dir / "outcome.json").write_text(
            json.dumps({**base, **empty, **outcome}), encoding="utf-8"
        )
    store.save_run(
        RunRecord(
            run_id=run_id,
            workspace=str(workspace.resolve()),
            launcher=["sh", "scio.sh"],
            argv=["build", "--product", "graphcalc"],
            env={},
            started_at=started.timestamp(),
            status=RunStatus.RUNNING if ended is None else RunStatus.EXITED,
            ended_at=None if ended is None else ended.timestamp(),
            exit_code=exit_code,
            run_dir=str(run_dir),
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
            outcome_path=str(run_dir / "outcome.json"),
        )
    )


PAUSED = {"status": "paused", "exit_code": 0}
HALTED = {"status": "halted", "exit_code": 1, "exit_reason": "integration_failed"}


def test_graphcalc_stall_time_runs_from_a_human_stop_to_the_next_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 4, "time stalled waiting on you": in the recovery supervisor's store, a run of
    the product's workspace that ended at a human stop (the supervisor's own
    ``classify(read_evidence(run))``: SCIO's outcome says ``paused``) is stalled from its end
    to the next run's start in the same workspace. A run that halted is not a human stop; a
    run of another workspace does not end the stall; the last human stop with no run after
    it is still stalled now. The store is read ``mode=ro`` and is left as it was."""
    from recovery.store import Store

    product, other = tmp_path / "graphcalc-run", tmp_path / "other-run"
    product.mkdir()
    other.mkdir()
    t0 = NOW - 10 * H
    db = tmp_path / "recovery" / "state.sqlite"
    with closing(Store(db)) as store:
        _store_run(store, tmp_path, "run-1", product, t0, t0 + 1 * H, PAUSED, 0)
        _store_run(store, tmp_path, "run-x", other, t0 + 2 * H, t0 + 2.5 * H, PAUSED, 0)
        _store_run(store, tmp_path, "run-2", product, t0 + 3 * H, t0 + 4 * H, HALTED, 1)
        _store_run(store, tmp_path, "run-3", product, t0 + 5 * H, t0 + 6 * H, PAUSED, 0)
    before = db.read_bytes()
    runner = FakeRunner(gh=_gh(PRS, RUNS, []))

    data = _metrics(capsys, ["--root", str(tmp_path), "--product-root", str(product)], runner)
    stall = data["graphcalc"]
    # run-1's end to run-2's start (2 h), and run-3's end to now (4 h): 6 h in all.
    assert stall["stall_hours"] == pytest.approx(6.0)
    assert stall["stalls"] == 2 and stall["stalled_now"] is True
    assert "ended_at" in stall["sources"]["stall_hours"]
    assert "started_at" in stall["sources"]["stall_hours"]
    assert db.read_bytes() == before

    # A window that starts inside the first stall counts only its part in the window.
    short = ["--root", str(tmp_path), "--product-root", str(product), "--days", str(8 / 24)]
    data = _metrics(capsys, short, runner)
    assert data["graphcalc"]["stall_hours"] == pytest.approx(5.0)

    # A next run after the last stop ends it: no longer stalled now.
    with closing(Store(db)) as store:
        _store_run(store, tmp_path, "run-4", product, t0 + 9 * H, None, None, None)
    data = _metrics(capsys, ["--root", str(tmp_path), "--product-root", str(product)], runner)
    assert data["graphcalc"]["stall_hours"] == pytest.approx(5.0)
    assert data["graphcalc"]["stalled_now"] is False


def test_graphcalc_features_cost_and_escalations_come_from_the_attempts() -> None:
    """Features passed per day, cost per passed feature (every attempt's USD over the
    features passed), and the escalation share: an attempt escalated when it ended at a human
    stop, the task graph's ``escalate_human`` or ``advisor_cap`` (its ``halt`` node
    interrupts on both), or waits on a question now. A crash and any other failure count
    apart, as failed with no human stop. Each attempt is dated by its thread's latest
    checkpoint; one outside the window, or still running, is out of the shares."""
    metrics = load("metrics")
    window = metrics.Window(NOW - 7 * D, NOW)
    a = metrics.Attempt
    attempts = [
        a("P1", 1, "b-p1-a1", "passed", "success", 2.0, NOW - 6 * D),
        a("P2", 1, "b-p2-a1", "failed", "max_iters", 3.0, NOW - 5 * D),
        a("P2", 2, "b-p2-a2", "passed", "success", 1.0, NOW - 4 * D),
        a("P3", 1, "b-p3-a1", "skipped", "escalate_human", 1.5, NOW - 3 * D),
        a("P4", 1, "b-p4-a1", "waiting", "advisor_cap", 0.5, NOW - 1 * H),
        a("P5", 1, "b-p5-a1", "running", None, 0.25, NOW - 2 * H),
        a("P6", 1, "b-p6-a1", "crashed", None, 0.0, NOW - 3 * H),
        a("P7", 1, "b-p7-a1", "failed", "advisor_cap", 0.75, NOW - 2 * D),  # answered: retry
        a("P8", 1, "b-p8-a1", "waiting", None, 0.5, NOW - 4 * H),  # a design question
        a("P0", 1, "b-p0-a1", "passed", "success", 9.0, NOW - 9 * D),  # before the window
    ]
    out = metrics.attempt_numbers(attempts, window)
    assert out["features_passed"] == 2
    assert out["features_per_day"] == pytest.approx(2 / 7)
    assert out["attempts_usd"] == pytest.approx(9.5)
    assert out["usd_per_feature"] == pytest.approx(9.5 / 2)
    assert out["attempts"] == 8  # ended in the window; the running one is not ended
    assert out["escalated"] == 4
    assert out["escalation_share"] == pytest.approx(4 / 8)
    assert out["escalated_by_reason"] == {"advisor_cap": 2, "escalate_human": 1, "question": 1}
    assert out["failed_no_human_stop"] == 2
    assert out["failed_by_reason"] == {"crashed": 1, "max_iters": 1}


def test_a_slip_is_counted_in_the_machine_log_not_the_repo(
    tmp_path: Path,
    machine_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invariant 5, "Slips ... become counts, not full ledger entries": ``scripts/slip.py
    "<kind>" "<what happened>"`` appends ``{at, kind, what, cwd}`` to ``slips.log`` in the
    machine directory and leaves the repository it runs in untouched; ``scripts/metrics.py``
    counts the slips in the window by kind, and the hook refusals by rule (W-18), counting a
    refusal that two hook registrations logged twice once (I-231)."""
    hub = make_hub(tmp_path)
    repo = clone(hub, tmp_path / "SCIO")
    monkeypatch.chdir(repo)
    slip = load("slip")
    assert slip.main(["heredoc", "a heredoc ate a backslash; rewritten with the Write tool"]) == 0
    assert slip.main(["wrong-cwd", "ran git in the main checkout; nothing changed"]) == 0
    assert slip.main(["heredoc", "the same again"]) == 0
    assert "slip: counted" in capsys.readouterr().out
    assert slip.main(["", "no kind"]) == 2

    log = machine_dir / "slips.log"
    raw = log.read_bytes()
    assert b"\r" not in raw
    entries = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert [e["kind"] for e in entries] == ["heredoc", "wrong-cwd", "heredoc"]
    assert entries[0]["what"].startswith("a heredoc ate a backslash")
    assert same(entries[0]["cwd"], repo)
    assert set(entries[0]) == {"at", "kind", "what", "cwd"}
    assert git(repo, "status", "--porcelain", "--ignored") == ""
    assert not list(repo.rglob("slips.log"))

    now = datetime.now(UTC) + timedelta(seconds=5)
    old = {"at": _iso(now - 30 * D), "kind": "heredoc", "what": "old", "cwd": "x"}
    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(old) + "\n")
    refusal = {"at": _iso(now - 1 * H), "rule": "backslash", "command": "x", "cwd": "y"}
    with (machine_dir / "hook-refusals.log").open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(refusal) + "\n")
        f.write(json.dumps(refusal) + "\n")  # the user-level and the project hook (I-231)
        f.write(json.dumps({**refusal, "rule": "stash", "command": "git stash"}) + "\n")
    data = _metrics(capsys, ["--root", str(repo)], FakeRunner(gh=_gh(PRS, RUNS, [])), now=now)
    assert data["slips"]["total"] == 3
    assert data["slips"]["by_kind"] == {"heredoc": 2, "wrong-cwd": 1}
    refusals = data["hook_refusals"]
    assert refusals["by_rule"] == {"backslash": 1, "stash": 1}
    assert refusals["total"] == 2 and refusals["duplicates_dropped"] == 1


def test_write_keeps_the_numbers_in_the_main_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--write`` saves the same numbers to the main checkout's ``.scio/metrics.json``, with
    ``computed_at``, from any worktree; the table names each number."""
    metrics = load("metrics")
    hub = make_hub(tmp_path)
    scio = clone(hub, tmp_path / "SCIO")
    wt = tmp_path / "wt"
    git(scio, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    commit(wt, {"x.py": "x = 1\n"}, "x")
    runner = FakeRunner(gh=_gh(PRS, RUNS, []))
    printed = _metrics(capsys, ["--root", str(wt)], runner)

    assert metrics.main(["--root", str(wt), "--write"], runner=runner, now=NOW) == 0
    table = capsys.readouterr().out
    path = scio / ".scio" / "metrics.json"
    raw = path.read_bytes()
    assert b"\r" not in raw
    saved = json.loads(raw)
    assert saved["computed_at"] == "2026-11-15T12:00:00Z" == printed["computed_at"]
    assert saved == printed
    assert not (wt / ".scio" / "metrics.json").exists()
    assert "lead time" in table and "rework 1/3" in table and "CI" in table and "gate" in table
    assert str(path) in table or path.as_posix() in table


def test_a_source_that_cannot_be_read_says_so_and_the_rest_still_counts(
    tmp_path: Path, machine_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    offline = FakeRunner(gh=lambda args: (1, "error connecting to api.github.com"))
    _gate_line(machine_dir / "gate-runs.log", NOW - 1 * H, "quick", 3.0, True)
    data = _metrics(capsys, ["--root", str(tmp_path)], offline)
    assert "api.github.com" in data["prs"]["error"]
    assert "api.github.com" in data["ci"]["error"]
    assert data["gate"]["minutes"]["n"] == 1


def test_metrics_and_slip_load_with_the_standard_library_alone() -> None:
    """The ``Stop`` hook runs ``session_state.py`` with the system ``python``, and it reads
    the cached numbers through ``metrics.py``'s renderer; ``slip.py`` must be instant. So
    neither may import SCIO, the supervisor or a third-party package at the top: ``-S``
    leaves out site-packages, and ``--help`` returns before any work."""
    for name in ("metrics.py", "slip.py"):
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(SCRIPTS / name), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (name, proc.stderr)
