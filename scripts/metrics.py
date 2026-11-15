"""The bottleneck audit as numbers: DORA metrics, adapted (issue #141, part 2).

    uv run python scripts/metrics.py [--days 7] [--product-root PATH] [--json] [--write]

The owner's words (#141): "For SCIO PRs: lead time (open to merge), gate and CI time, and the
share of PRs that need rework. For graphcalc: features per day, cost per feature, the share of
attempts that escalate, and time stalled waiting on you."

Over the last ``--days`` (7), it reads:
- **SCIO PRs** merged in the window (``gh pr list --state merged``): the lead time, ``createdAt``
  to ``mergedAt``; the share labelled ``low-risk``; the **rework** share, PRs with at least one
  failed CI run (``failure``, ``timed_out`` or ``startup_failure``) of ``ci.yml`` on their head
  branch, from a ``pull_request`` event between their opening and their merge. ``gh run list``
  shows a run's latest attempt only, so a run re-run green hides its first verdict: those PRs
  are listed apart as re-runs.
- **CI time:** the completed ``ci.yml`` runs created in the window, ``startedAt`` to
  ``updatedAt``.
- **Gate time:** ``gate-runs.log`` in SCIO's per-machine directory (``scripts/gate.py``'s
  ``machine_dir``), one line per finished gate.
- **Counts:** the hook refusals by rule (``hook-refusals.log``, W-18; a line two hook
  registrations wrote twice counts once, I-231) and the slips by kind (``slips.log``,
  ``scripts/slip.py``), in the same directory.
- **graphcalc**, with ``--product-root`` (the workspace ``scio build`` runs in, read-only):
  features passed per day and the cost per passed feature, from the build threads' feature
  runs (``scio.dashboard.data``); the share of attempts that ended at a human stop
  (``escalate_human``, ``advisor_cap``, or a question pending now), with the attempts that
  failed with no human stop (a crash, ``max_iters``, ``budget``) counted apart; and the time
  stalled waiting on the owner, from the recovery supervisor's run records
  (``%LOCALAPPDATA%/SCIO/recovery/state.sqlite``, opened ``mode=ro``).

Medians are the middle value (the mean of the two middle ones for an even count); p90 is the
nearest rank, the smallest value at or above 90 % of them. Each number carries its ``source``:
where its timestamps come from. A source that cannot be read says so, and the rest still
counts. ``--json`` prints the numbers as JSON; ``--write`` also saves them, with
``computed_at``, to the main checkout's ``.scio/metrics.json``, which ``session_state.py``
shows with its age. It never computes them itself: the ``Stop`` hook has 30 seconds.

Standard library and the ``gh`` CLI at the top; the graphcalc readers (SCIO's dashboard data
layer and the supervisor's classifier) are imported only when ``--product-root`` is given, so
``session_state.py`` loads :func:`summary_rows` with the system Python.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
METRICS = Path(".scio") / "metrics.json"
CI_WORKFLOW = "ci.yml"
LOW_RISK = "low-risk"
"""The label ``scripts/land.py`` puts on a PR that changes only docs or tests (#141)."""
PR_LIMIT = 500
RUN_LIMIT = 1000
PR_FIELDS = "number,title,createdAt,mergedAt,labels,headRefName"
RUN_FIELDS = "databaseId,headBranch,event,status,conclusion,attempt,createdAt,startedAt,updatedAt"
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
"""A CI run's conclusions that mean the PR needed rework; ``cancelled`` is not one (a newer
push cancels the older run)."""
GATE_LOG = "gate-runs.log"
REFUSAL_LOG = "hook-refusals.log"
SLIP_LOG = "slips.log"
HUMAN_STOP_EXITS = frozenset({"escalate_human", "advisor_cap"})
"""The task graph's exits that stop for a human: its ``halt`` node interrupts on these two
(``scio/graph/nodes/checkpoint_gate.py``). An attempt that ended on one escalated."""
HUMAN_STOP_STATUSES = frozenset({"waiting", "parked"})
"""An attempt at a stop now (a pending interrupt: a question to the owner), or parked on a
design question (#141, part 5), also escalated, whatever its exit reason."""
NOT_ENDED = frozenset({"running", "in_progress"})

SOURCES = {
    "prs": (
        "GitHub: each PR merged in the window, createdAt to mergedAt; rework: a failed run of "
        "ci.yml on its head branch (event pull_request) created between its createdAt and "
        "mergedAt; low-risk: its labels"
    ),
    "ci": "GitHub: the completed runs of ci.yml created in the window, startedAt to updatedAt",
    "gate": (
        "gate-runs.log in the machine directory: each finished gate's at (UTC) and minutes "
        "(the gate's own clock)"
    ),
    "hook_refusals": "hook-refusals.log in the machine directory: each refusal's at (UTC)",
    "slips": "slips.log in the machine directory: each slip's at (UTC)",
}
GRAPHCALC_SOURCES = {
    "features_passed": (
        "the build threads' feature runs (scio.dashboard.data), each attempt dated by its "
        "thread's latest checkpoint ts"
    ),
    "usd_per_feature": (
        "every attempt's usd (the build's feature-run record, else the attempt thread's "
        "budget) dated in the window, over the features passed in it"
    ),
    "escalation_share": (
        "attempts that ended at a human stop (exit escalate_human or advisor_cap, or waiting "
        "or parked on a question now), over the attempts that ended in the window; the other "
        "attempts that did not pass (crashes, max_iters, budget, errors) are failed (no human "
        "stop)"
    ),
    "stall_hours": (
        "the recovery supervisor's store (state.sqlite, mode=ro): a run of this workspace that "
        "ended at a human stop (recovery.classify), from its ended_at to the next run's "
        "started_at, or to now"
    ),
}


# Loading the session tools beside this file.


def _session_state() -> ModuleType:
    """``session_state.py`` beside this file: the runner and the main checkout. Loaded by path
    under the session tools' key, as ``land.py`` loads it."""
    path = SCRIPTS / "session_state.py"
    key = f"_session_tools_session_state_{abs(hash(str(path)))}"
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


def machine_dir() -> Path:
    """``scripts/gate.py``'s per-machine directory: one definition, read at each call."""
    path: Path = _session_state().sibling("gate").machine_dir()
    return path


# Time and statistics.


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 time (``2026-11-15T03:39:45Z``) in UTC; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    @property
    def days(self) -> float:
        return (self.until - self.since).total_seconds() / 86400

    def holds(self, at: datetime | None) -> bool:
        return at is not None and self.since <= at <= self.until

    def overlap_hours(self, start: datetime, end: datetime) -> float:
        """How much of ``start`` to ``end`` lies in the window, in hours."""
        lo, hi = max(start, self.since), min(end, self.until)
        return max(0.0, (hi - lo).total_seconds() / 3600)


def stats(values: Iterable[float]) -> dict[str, Any]:
    """``{"n", "median", "p90"}``; p90 by nearest rank. None for both without values."""
    vals = sorted(values)
    if not vals:
        return {"n": 0, "median": None, "p90": None}
    p90 = vals[math.ceil(0.9 * len(vals)) - 1]
    return {"n": len(vals), "median": round(statistics.median(vals), 2), "p90": round(p90, 2)}


def share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


# SCIO's PRs and CI.


def _gh_json(runner: Any, root: Path, args: list[str]) -> list[dict[str, Any]] | str:
    """A ``gh`` call's JSON list, or why it could not be read."""
    proc = runner.run(["gh", *args], cwd=root, timeout=120)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"unavailable (gh exit {proc.returncode}: {why[-1] if why else 'no output'})"
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return "unavailable (gh printed no JSON)"
    if not isinstance(data, list):
        return "unavailable (unexpected JSON)"
    return [d for d in data if isinstance(d, dict)]


def _labels(pr: dict[str, Any]) -> set[str]:
    return {str(lb.get("name")) for lb in pr.get("labels") or [] if isinstance(lb, dict)}


def pr_numbers(
    merged: list[dict[str, Any]], runs: list[dict[str, Any]] | None, truncated: bool
) -> dict[str, Any]:
    """The PRs merged in the window: lead time, low-risk share, rework and re-runs. ``runs``
    None: the CI runs could not be read, so rework is unknown."""
    lead: list[float] = []
    low_risk = rework = reruns = 0
    rework_prs: list[int] = []
    rerun_prs: list[int] = []
    for pr in merged:
        opened, done = parse_time(pr.get("createdAt")), parse_time(pr.get("mergedAt"))
        if opened is None or done is None:
            continue
        lead.append((done - opened).total_seconds() / 3600)
        if LOW_RISK in _labels(pr):
            low_risk += 1
        own = [
            r
            for r in runs or []
            if r.get("headBranch") == pr.get("headRefName")
            and r.get("event") == "pull_request"
            and (at := parse_time(r.get("createdAt"))) is not None
            and opened <= at <= done
        ]
        if any(r.get("conclusion") in FAILED for r in own):
            rework += 1
            rework_prs.append(int(pr.get("number") or 0))
        if any(int(r.get("attempt") or 1) > 1 for r in own):
            reruns += 1
            rerun_prs.append(int(pr.get("number") or 0))
    n = len(lead)
    known = runs is not None
    return {
        "merged": n,
        "truncated": truncated,
        "lead_time_hours": stats(lead),
        "low_risk": low_risk,
        "low_risk_share": share(low_risk, n),
        "rework": rework if known else None,
        "rework_share": share(rework, n) if known else None,
        "rework_prs": sorted(rework_prs),
        "reruns": reruns if known else None,
        "rerun_prs": sorted(rerun_prs),
        "source": SOURCES["prs"],
    }


def ci_numbers(runs: list[dict[str, Any]], window: Window, truncated: bool) -> dict[str, Any]:
    minutes: list[float] = []
    for run in runs:
        if run.get("status") != "completed" or not window.holds(parse_time(run.get("createdAt"))):
            continue
        start, end = parse_time(run.get("startedAt")), parse_time(run.get("updatedAt"))
        if start is not None and end is not None:
            minutes.append((end - start).total_seconds() / 60)
    return {
        "runs": len(minutes),
        "minutes": stats(minutes),
        "truncated": truncated,
        "source": SOURCES["ci"],
    }


def github_numbers(runner: Any, root: Path, window: Window) -> dict[str, Any]:
    out: dict[str, Any] = {}
    search = f"merged:>={window.since:%Y-%m-%d}"
    pr_args = ["pr", "list", "--state", "merged", "--search", search]
    prs = _gh_json(runner, root, [*pr_args, "--limit", str(PR_LIMIT), "--json", PR_FIELDS])
    merged = (
        [p for p in prs if window.holds(parse_time(p.get("mergedAt")))]
        if isinstance(prs, list)
        else []
    )
    opened = [at for p in merged if (at := parse_time(p.get("createdAt"))) is not None]
    oldest = min([window.since, *opened])
    run_args = ["run", "list", "--workflow", CI_WORKFLOW, "--created", f">={oldest:%Y-%m-%d}"]
    runs = _gh_json(runner, root, [*run_args, "--limit", str(RUN_LIMIT), "--json", RUN_FIELDS])
    run_list = runs if isinstance(runs, list) else None
    if isinstance(prs, str):
        out["prs"] = {"error": prs, "source": SOURCES["prs"]}
    else:
        out["prs"] = pr_numbers(merged, run_list, len(prs) >= PR_LIMIT)
        if run_list is None:
            out["prs"]["rework_error"] = runs
    if run_list is None:
        out["ci"] = {"error": runs, "source": SOURCES["ci"]}
    else:
        out["ci"] = ci_numbers(run_list, window, len(run_list) >= RUN_LIMIT)
    return out


# The machine logs.


def read_log(path: Path) -> list[dict[str, Any]]:
    """A JSON-lines log's objects; a line that is not one is skipped, a missing log is none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _in(entries: list[dict[str, Any]], window: Window) -> list[dict[str, Any]]:
    return [e for e in entries if window.holds(parse_time(e.get("at")))]


def _ranked(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def gate_numbers(entries: list[dict[str, Any]], window: Window) -> dict[str, Any]:
    runs = _in(entries, window)
    minutes = [float(m) for e in runs if isinstance(m := e.get("minutes"), int | float)]
    passed = sum(1 for e in runs if e.get("passed") is True)
    return {
        "runs": len(runs),
        "minutes": stats(minutes),
        "passed": passed,
        "pass_rate": share(passed, len(runs)),
        "by_mode": _ranked(Counter(str(e.get("mode") or "?") for e in runs)),
        "source": SOURCES["gate"],
    }


def count_numbers(
    entries: list[dict[str, Any]], window: Window, key: str, *, dedupe: bool = False
) -> dict[str, Any]:
    """The entries in the window by ``key``. ``dedupe``: an exact repeat (the same time,
    rule, command and directory) is one event logged twice, by the push-gate hook registered
    at user level and at project level at once (I-231)."""
    kept = _in(entries, window)
    dropped = 0
    if dedupe:
        unique = {json.dumps(e, sort_keys=True): e for e in kept}
        dropped, kept = len(kept) - len(unique), list(unique.values())
    out: dict[str, Any] = {
        "total": len(kept),
        f"by_{key}": _ranked(Counter(str(e.get(key) or "?") for e in kept)),
    }
    if dedupe:
        out["duplicates_dropped"] = dropped
    return out


# graphcalc.


@dataclass(frozen=True)
class Attempt:
    """One feature attempt of a ``scio build``: its build's verdict (``passed``, ``failed``,
    ``skipped``) or, with none recorded yet, its thread's (``running``, ``waiting``,
    ``crashed``, ``halted``, ``stopped``)."""

    feature_id: str
    attempt: int
    thread_id: str
    status: str
    exit_reason: str | None
    usd: float
    ended_at: datetime | None
    """Its thread's latest checkpoint; None when the thread is not in the DB."""


def escalated(a: Attempt) -> bool:
    """The attempt ended at a human stop: :data:`HUMAN_STOP_EXITS` or
    :data:`HUMAN_STOP_STATUSES` (the coordinator's decision, #141)."""
    return a.exit_reason in HUMAN_STOP_EXITS or a.status in HUMAN_STOP_STATUSES


def attempt_numbers(attempts: list[Attempt], window: Window) -> dict[str, Any]:
    """Features passed (distinct features with a passed attempt dated in the window) per day,
    every dated attempt's USD over them, the share of the attempts that ended which escalated
    (:func:`escalated`), and the others that did not pass, failed with no human stop (a
    crash, ``max_iters``, ``budget``, an error), each with their reasons."""
    dated = [a for a in attempts if window.holds(a.ended_at)]
    passed = {a.feature_id for a in dated if a.status == "passed"}
    usd = sum(a.usd for a in dated)
    ended = [a for a in dated if a.status not in NOT_ENDED]
    up = [a for a in ended if escalated(a)]
    failed = [a for a in ended if a.status != "passed" and not escalated(a)]
    failed_by = Counter(a.exit_reason or a.status for a in failed)
    return {
        "features_passed": len(passed),
        "features_per_day": len(passed) / window.days if window.days > 0 else None,
        "attempts": len(ended),
        "attempts_usd": round(usd, 4),
        "usd_per_feature": round(usd / len(passed), 4) if passed else None,
        "escalated": len(up),
        "escalation_share": share(len(up), len(ended)),
        "escalated_by_reason": dict(
            sorted(Counter(a.exit_reason or "question" for a in up).items())
        ),
        "failed_no_human_stop": len(failed),
        "failed_by_reason": dict(sorted(failed_by.items())),
        "undated_attempts": sum(1 for a in attempts if a.ended_at is None),
    }


def read_attempts(product_root: Path) -> list[Attempt] | str:
    """Every feature attempt of every build thread in ``product_root``'s checkpoint DB,
    through the dashboard's read-only data layer; or why there are none."""
    from scio.dashboard.data import CheckpointDataSource

    src = CheckpointDataSource.from_roots([product_root])
    [root] = src.roots()
    if not root.present:
        return f"no checkpoint DB at {root.checkpoints}"
    summaries = src.list_threads(root.name)
    by_id = {s.thread_id: s for s in summaries}
    found: dict[str, Attempt] = {}
    for build in (s for s in summaries if s.kind == "build"):
        detail = src.thread(root.name, build.thread_id)
        for row in detail.build.features if detail.build else []:
            if row.thread_id in found:
                continue
            t = by_id.get(row.thread_id)
            found[row.thread_id] = Attempt(
                row.feature_id,
                row.attempt,
                row.thread_id,
                row.status,
                row.exit_reason,
                row.usd,
                t.updated_at if t else None,
            )
        for link in detail.feature_threads:  # the attempts with no run recorded yet
            if link.thread_id in found:
                continue
            t = by_id.get(link.thread_id)
            found[link.thread_id] = Attempt(
                link.feature_id,
                link.attempt,
                link.thread_id,
                link.status,
                t.exit_reason if t else None,
                t.usd if t else 0.0,
                t.updated_at if t else None,
            )
    return list(found.values())


def read_stalls(product_root: Path, window: Window) -> dict[str, Any]:
    """The time ``product_root``'s supervised runs stood at a human stop, in the window: a run
    that ended there (the supervisor's own ``classify(read_evidence(run))``), until the next
    run of the workspace started, or until now. The store is opened ``mode=ro``, as the
    dashboard opens it, and never written."""
    from recovery.classify import classify
    from recovery.evidence import read_evidence
    from recovery.model import FailureClass, RunRecord, RunStatus
    from scio.dashboard import supervisor

    path = supervisor.store_path()
    if not path.is_file():
        return {"error": f"no supervisor store at {path}"}
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        supervisor.check_schema(conn)
        try:
            rows = conn.execute(
                "select data from runs where status != ? order by started_at",
                (RunStatus.REFUSED.value,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            rows = []
    workspace = str(product_root.resolve())
    runs: list[RunRecord] = []
    for (data,) in rows:
        try:
            run = RunRecord.from_dict(json.loads(data))
        except (TypeError, ValueError, KeyError):
            continue
        if supervisor.same_path(run.workspace, workspace):
            runs.append(run)
    runs.sort(key=lambda r: r.started_at)
    hours, stalls, stalled_now = 0.0, 0, False
    for i, run in enumerate(runs):
        if run.status == RunStatus.RUNNING or run.ended_at is None:
            continue
        if classify(read_evidence(run)).failure_class != FailureClass.HUMAN_STOP:
            continue
        nxt = runs[i + 1] if i + 1 < len(runs) else None
        start = datetime.fromtimestamp(run.ended_at, tz=UTC)
        end = window.until if nxt is None else datetime.fromtimestamp(nxt.started_at, tz=UTC)
        stalled_now = stalled_now or nxt is None
        part = window.overlap_hours(start, end)
        if part > 0:
            hours += part
            stalls += 1
    return {
        "stall_hours": round(hours, 3),
        "stalls": stalls,
        "stalled_now": stalled_now,
        "supervised_runs": len(runs),
    }


def graphcalc_numbers(product_root: Path, window: Window) -> dict[str, Any]:
    out: dict[str, Any] = {
        "product_root": str(product_root),
        "sources": GRAPHCALC_SOURCES,
        "errors": [],
    }
    try:
        attempts = read_attempts(product_root)
    except Exception as exc:  # a reader that fails is one line; the stall time still counts
        attempts = f"the checkpoint DB could not be read ({type(exc).__name__}: {exc})"
    if isinstance(attempts, str):
        out["errors"].append(attempts)
    else:
        out.update(attempt_numbers(attempts, window))
    try:
        stalls = read_stalls(product_root, window)
    except Exception as exc:
        why = f"{type(exc).__name__}: {exc}"
        stalls = {"error": f"the supervisor's store could not be read ({why})"}
    if "error" in stalls:
        out["errors"].append(stalls["error"])
    else:
        out.update(stalls)
    return out


# Computing, rendering, writing.


def compute(
    root: Path, runner: Any, window: Window, product_root: Path | None = None
) -> dict[str, Any]:
    machine = machine_dir()
    span = {"days": round(window.days, 3), "since": iso(window.since), "until": iso(window.until)}
    data: dict[str, Any] = {"computed_at": iso(window.until), "window": span}
    data.update(github_numbers(runner, root, window))
    data["gate"] = gate_numbers(read_log(machine / GATE_LOG), window)
    refusals = read_log(machine / REFUSAL_LOG)
    data["hook_refusals"] = {
        **count_numbers(refusals, window, "rule", dedupe=True),
        "source": SOURCES["hook_refusals"],
    }
    data["slips"] = {
        **count_numbers(read_log(machine / SLIP_LOG), window, "kind"),
        "source": SOURCES["slips"],
    }
    data["graphcalc"] = graphcalc_numbers(product_root, window) if product_root else None
    return data


def _pct(part: object, whole: object) -> str:
    if not isinstance(part, int) or not isinstance(whole, int) or not whole:
        return f"{part}/{whole}"
    return f"{part}/{whole} ({part / whole:.0%})"


def _spread(m: dict[str, Any] | None, unit: str) -> str:
    if not m or not m.get("n"):
        return "none"
    return f"median {m['median']:.1f} {unit}, p90 {m['p90']:.1f} {unit}"


def _top(counts: dict[str, int] | None) -> str:
    return ", ".join(f"{k} {v}" for k, v in (counts or {}).items())


def summary_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The numbers as ``(label, text)`` rows, for this script's table and for
    ``session_state.py``'s view of the cached file. Missing parts read as unavailable."""
    rows: list[tuple[str, str]] = []
    prs = data.get("prs") or {}
    if "error" in prs:
        rows.append(("SCIO PRs", prs["error"]))
    else:
        n = prs.get("merged", 0)
        text = f"{n} merged; lead time {_spread(prs.get('lead_time_hours'), 'h')}"
        rework = prs.get("rework")
        text += f"; rework {_pct(rework, n)}" if rework is not None else "; rework unknown"
        if prs.get("reruns"):
            text += f"; re-run {prs['reruns']}"
        text += f"; low-risk {_pct(prs.get('low_risk', 0), n)}"
        if prs.get("truncated"):
            text += f" (truncated at {PR_LIMIT})"
        rows.append(("SCIO PRs", text))
    ci = data.get("ci") or {}
    if "error" in ci:
        rows.append(("CI", ci["error"]))
    else:
        runs = ci.get("runs", (ci.get("minutes") or {}).get("n", 0))
        text = f"{runs} runs; {_spread(ci.get('minutes'), 'min')}"
        rows.append(("CI", text + (f" (truncated at {RUN_LIMIT})" if ci.get("truncated") else "")))
    gate = data.get("gate") or {}
    runs = gate.get("runs", (gate.get("minutes") or {}).get("n", 0))
    text = f"{runs} runs; {_spread(gate.get('minutes'), 'min')}"
    rows.append(("gate", text + (f"; passed {_pct(gate.get('passed', 0), runs)}" if runs else "")))
    refusals, slips = data.get("hook_refusals") or {}, data.get("slips") or {}
    text = f"hook refusals {refusals.get('total', 0)}"
    text += f" ({_top(refusals.get('by_rule'))})" if refusals.get("by_rule") else ""
    text += f"; slips {slips.get('total', 0)}"
    text += f" ({_top(slips.get('by_kind'))})" if slips.get("by_kind") else ""
    rows.append(("counts", text))
    gc = data.get("graphcalc")
    if isinstance(gc, dict):
        parts: list[str] = []
        if "features_passed" in gc:
            per_day = gc.get("features_per_day")
            parts.append(
                f"{gc['features_passed']} features passed"
                + (f" ({per_day:.2f}/day)" if isinstance(per_day, int | float) else "")
            )
            usd = gc.get("usd_per_feature")
            parts.append(f"${usd:.2f} per feature" if usd is not None else "no feature passed")
            attempts = gc.get("attempts", 0)
            parts.append(f"escalated {_pct(gc.get('escalated', 0), attempts)}")
            if "failed_no_human_stop" in gc:
                failed = _pct(gc["failed_no_human_stop"], attempts)
                parts.append(f"failed (no human stop) {failed}")
        if "stall_hours" in gc:
            stall = f"stalled {gc['stall_hours']:.1f} h"
            parts.append(stall + (", stalled now" if gc.get("stalled_now") else ""))
        parts += [f"unavailable: {e}" for e in gc.get("errors") or []]
        rows.append(("graphcalc", "; ".join(parts)))
    return rows


def render(data: dict[str, Any], written: Path | None = None) -> str:
    window = data.get("window") or {}
    until = parse_time(window.get("until"))
    when = f"{until:%Y-%m-%d %H:%M} UTC" if until else "?"
    days = window.get("days")
    span = f"{days:g}" if isinstance(days, int | float) else "?"
    out = [f"SCIO metrics, the {span} days to {when}"]
    out += [f"  {label:<10} {text}" for label, text in summary_rows(data)]
    prs = data.get("prs") or {}
    details = []
    if prs.get("rework_prs"):
        details.append("rework " + ", ".join(f"#{n}" for n in prs["rework_prs"]))
    if prs.get("rerun_prs"):
        details.append("re-run " + ", ".join(f"#{n}" for n in prs["rerun_prs"]))
    if details:
        out.append(f"  {'PRs':<10} {'; '.join(details)}")
    out.append("sources")
    for key in ("prs", "ci", "gate", "hook_refusals", "slips"):
        out.append(f"  {key}: {SOURCES[key]}")
    gc = data.get("graphcalc")
    if isinstance(gc, dict):
        out += [f"  graphcalc {key}: {text}" for key, text in GRAPHCALC_SOURCES.items()]
    if written is not None:
        out.append(f"written to {written.as_posix()}")
    return "\n".join(out)


def write_cache(main: Path, data: dict[str, Any]) -> Path:
    """``<main>/.scio/metrics.json``, whole or not at all, with LF line endings."""
    path = main / METRICS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def _days(text: str) -> float:
    days = float(text)
    if not days > 0:
        raise argparse.ArgumentTypeError("--days must be above 0")
    return days


def main(argv: list[str] | None = None, runner: Any = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0] if __doc__ else None,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--days", type=_days, default=7.0, help="the window, back from now (7)")
    ap.add_argument(
        "--product-root",
        type=Path,
        help="a product workspace (graphcalc's: the graphcalc-run directory), read-only",
    )
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    ap.add_argument(
        "--write", action="store_true", help="also save them to <main>/.scio/metrics.json"
    )
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    ss = _session_state()
    ss.console()
    runner = runner or ss.Runner()
    until = now or datetime.now(UTC)
    window = Window(until - timedelta(days=args.days), until)
    data = compute(args.root, runner, window, args.product_root)
    written = None
    if args.write:
        written = write_cache(ss.main_checkout(args.root, runner), data)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(render(data, written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
