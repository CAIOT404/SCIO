"""`scripts/session_state.py` (issue #122, W-20 and W-28): the state-first inspection in one
command, and the handoff's facts block written from it.

The workspace below has what a session meets: a gated branch ahead and behind main, a dirty
branch whose gate is running, the two pinned checkouts behind main, a draft PR with a check
still running, open ``live-run`` and ``proposal`` issues, a supervised build and an incident,
and the Workflow v2 checklist with one item open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.session_tools_support import (
    FakeRunner,
    add_workflow_checklist,
    branch_commit,
    clone,
    commit,
    git,
    load,
    make_hub,
    same,
    stamp,
    tree,
    write,
)

RECOVERY_BUSY = """runs (2, newest first):
  run-20261114-a1  running  exit -  build --product C:/graphcalc --gh o/graphcalc
    started 2026-11-14 10:00:00 UTC, ended -
    dir: C:/state/runs/run-20261114-a1
  run-20261113-b2  exited  exit 0  build --product C:/graphcalc
    started 2026-11-13 09:00:00 UTC, ended 2026-11-13 11:00:00 UTC
    dir: -
open incidents (newest first):
  inc-20261114094856-5356c9  awaiting_merge  harness_fault  issue #105  PR #106
    a traceback in scio/loop/agent_loop.py
"""

PRS = [
    {
        "number": 118,
        "title": "feat: x",
        "headRefName": "feat/x",
        "baseRefName": "main",
        "isDraft": True,
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
        ],
    }
]
ISSUES = {
    "live-run": [{"number": 105, "title": "graphcalc: a traceback in the loop"}],
    "proposal": [{"number": 98, "title": "graphcalc: a tick recipe"}],
}


def _gh(args: list[str]) -> tuple[int, str]:
    if args[:2] == ["pr", "list"]:
        return 0, json.dumps(PRS)
    if args[:2] == ["issue", "list"]:
        return 0, json.dumps(ISSUES[args[args.index("--label") + 1]])
    return 1, f"unexpected gh {args}"


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, Path], FakeRunner, list[Path | None]]:
    hub = make_hub(tmp_path, ledger_last=5, guard_last=3)
    add_workflow_checklist(hub)
    git(hub, "add", "-A")
    git(hub, "commit", "-q", "-m", "the checklist")
    base = git(hub, "rev-parse", "HEAD")
    branch_commit(hub, "feat/x", {"x.py": "x = 1\n"})
    branch_commit(hub, "feat/y", {"y.py": "y = 1\n"})
    scio = clone(hub, tmp_path / "SCIO")
    wts = {
        "x": tmp_path / "wt-x",
        "y": tmp_path / "wt-y",
        "live": scio / ".scio" / "live-wt",
        "recovery": scio / ".scio" / "recovery-wt",
    }
    git(scio, "worktree", "add", "-q", "-b", "feat/x", str(wts["x"]), "origin/feat/x")
    git(scio, "worktree", "add", "-q", "-b", "feat/y", str(wts["y"]), "origin/feat/y")
    git(scio, "worktree", "add", "-q", "--detach", str(wts["live"]), base)
    git(scio, "worktree", "add", "-q", "--detach", str(wts["recovery"]), base)
    commit(wts["x"], {"x.py": "x = 2\n"}, "not pushed yet")  # one ahead
    stamp(wts["x"], tree(wts["x"]), mode="quick")
    stamp(wts["y"], "0" * 40)  # a stamp for some older tree
    write(wts["y"], {".scio/gate/RUNNING": "", "notes.txt": "dirty\n"})
    commit(hub, {"README.md": "# SCIO, moved on\n"}, "main moves")  # everything is one behind
    git(scio, "fetch", "-q", "origin")
    seen_cwd: list[Path | None] = []

    def python(argv: list[str], cwd: Path | None) -> tuple[int, str]:
        assert argv == ["-m", "recovery", "status"], argv
        seen_cwd.append(cwd)
        return 0, RECOVERY_BUSY

    return scio, wts, FakeRunner(gh=_gh, python=python), seen_cwd


def test_session_state_reports_worktrees_prs_ids_and_open_checklist_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ss = load("session_state")
    scio, wts, runner, recovery_cwd = _workspace(tmp_path)
    assert ss.main(["--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    def line(*parts: str) -> str:
        found = [ln for ln in lines if all(p in ln for p in parts)]
        assert len(found) == 1, (parts, out)
        return found[0]

    assert f"origin/main   {git(scio, 'rev-parse', '--short', 'origin/main')}" in out
    live_head = git(wts["live"], "rev-parse", "--short=7", "HEAD")
    assert f"live-wt {live_head}, behind main by 1" in out
    assert f"recovery-wt {live_head}, behind main by 1" in out
    assert "gated (quick)" in line("wt-x", "feat/x", "+2/-1")  # pushed one, then one more
    assert "dirty 0" in line("wt-x", "feat/x")
    y = line("wt-y", "feat/y")
    assert "stamp stale" in y and "dirty 1" in y and "GATE RUNNING" in y
    assert "no stamp" in line(".scio/live-wt", "(detached)")
    pr = line("#118")
    assert "feat/x -> main" in pr and "BLOCKED" in pr and "draft" in pr
    assert "checks: 1 pending, 1 success" in pr
    assert "#105  graphcalc: a traceback in the loop" in out
    assert "#98  graphcalc: a tick recipe" in out
    assert "running supervised runs: 1" in out and "run-20261114-a1  running" in out
    assert "inc-20261114094856-5356c9  awaiting_merge" in out
    assert len(recovery_cwd) == 1 and same(recovery_cwd[0], wts["recovery"])
    assert "next ids      I-006, guardrail 4" in out
    assert "workflow v2   1/2 closed" in out
    assert "open: W-02 · An open item" in out
    assert "W-01" not in out


def test_session_state_markdown_is_the_handoff_facts_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Stop hook runs ``--markdown --write --detach``: it returns at once and the
    background run writes the main checkout's ``.scio/session-state.md``; nothing printed."""
    ss = load("session_state")
    scio, wts, runner, _ = _workspace(tmp_path)

    assert ss.main(["--root", str(wts["x"]), "--markdown", "--write", "--detach"], runner) == 0
    [(argv, _cwd, log, _env)] = runner.spawned
    assert argv[1].endswith("session_state.py") and "--markdown" in argv and "--write" in argv
    assert same(log, scio / ".scio" / "session-state.log")
    assert runner.gh_calls() == [], "the hook itself reads nothing; the background run does"

    # The background run, from any worktree, writes to the main checkout.
    assert ss.main(argv[2:], runner) == 0
    assert capsys.readouterr().out == ""
    facts = scio / ".scio" / "session-state.md"
    raw = facts.read_bytes()
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    assert text.startswith("# Session state\n")
    assert f"- **origin/main:** `{git(scio, 'rev-parse', '--short', 'origin/main')}`" in text
    assert "- **live-wt:** " in text and "behind main by 1" in text
    assert "- **recovery-wt:** " in text and "(moves only on the owner's word)" in text
    assert "- **Next ids:** I-006, guardrail 4" in text
    assert "| Worktree | Branch | Head | Ahead | Behind | Gate | Dirty |" in text
    assert "| feat/x | " in text and "gated (quick)" in text
    assert "stamp stale, RUNNING" in text
    assert "| #118 feat: x | feat/x | main | BLOCKED | yes | 1 pending, 1 success |" in text
    assert "- `live-run` #105: graphcalc: a traceback in the loop" in text
    assert "- open incidents: 1" in text
    assert "## Workflow v2\n\n1/2 closed" in text and "- W-02 · An open item" in text

    # The same facts print with --markdown alone, for a handoff written by hand.
    assert ss.main(["--root", str(scio), "--markdown"], runner) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("# Session state\n") and "## Open PRs" in printed


def test_a_source_that_cannot_be_read_says_so_and_the_rest_still_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ss = load("session_state")
    scio, _wts, _, _ = _workspace(tmp_path)
    offline = FakeRunner(
        gh=lambda args: (1, "error connecting to api.github.com"),
        python=lambda argv, cwd: (1, "Traceback: no module named recovery"),
    )
    assert ss.main(["--root", str(scio)], runner=offline) == 0
    out = capsys.readouterr().out
    assert "open PRs      unavailable (gh exit 1: error connecting to api.github.com)" in out
    assert "unavailable: exit 1: Traceback: no module named recovery" in out
    assert "next ids      I-006, guardrail 4" in out
