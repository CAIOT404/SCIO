"""`scripts/land.py` (issue #122, W-22 to W-26, W-29): a PR is landed up to the owner's merge,
and one command finishes the tail after it.

GitHub is the ``hub`` repository plus a fake ``gh`` that reads PR heads from it and makes
``update-branch`` and merges there, as GitHub does; ``land.py``'s own ``git fetch`` and
``git pull`` are real, its ``git push`` is only recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.session_tools_support import (
    FakeRunner,
    branch_commit,
    clone,
    commit,
    git,
    load,
    make_hub,
    merge_into,
    same,
    stamp,
    tree,
    write,
)

TEMPLATE_BODY = """## Summary
<!-- What changed and why, in 1-3 sentences. -->

Closes #90

## Test evidence
```
<command>
<result summary>
```

## Docs updated
- [x] N/A: no user-facing or architectural change
"""


class FakeGitHub:
    """``gh`` over the hub: a PR's head is the hub branch's commit, ``update-branch`` merges
    main into it there, and every edit is kept for the test to read."""

    def __init__(self, hub: Path, prs: dict[int, dict[str, Any]]) -> None:
        self.hub = hub
        self.prs = prs
        self.merged_heads: dict[str, int] = {}
        self.bodies: list[str] = []
        self.releases: dict[str, str] = {}

    def view(self, n: int) -> dict[str, Any]:
        pr = dict(self.prs[n])
        if pr.get("state", "OPEN") == "OPEN":
            pr["headRefOid"] = git(self.hub, "rev-parse", pr["headRefName"])
        pr.setdefault("state", "OPEN")
        pr["mergeStateStatus"] = "DRAFT" if pr.get("isDraft") else pr.get("status", "CLEAN")
        return pr

    def __call__(self, args: list[str]) -> tuple[int, str]:
        if args[:2] == ["pr", "view"]:
            n, fields = int(args[2]), args[args.index("--json") + 1].split(",")
            pr = self.view(n)
            return 0, json.dumps({f: pr.get(f) for f in fields})
        if args[:2] == ["pr", "list"] and "--head" in args:
            head = args[args.index("--head") + 1]
            merged = [{"number": self.merged_heads[head]}] if head in self.merged_heads else []
            return 0, json.dumps(merged)
        if args[:2] == ["pr", "list"] and "--base" in args:
            base = args[args.index("--base") + 1]
            found = [
                {"number": n, "headRefName": p["headRefName"], "title": p.get("title", "")}
                for n, p in self.prs.items()
                if p.get("baseRefName") == base and p.get("state", "OPEN") == "OPEN"
            ]
            return 0, json.dumps(found)
        if args[:2] == ["pr", "edit"] and "--base" in args:
            self.prs[int(args[2])]["baseRefName"] = args[args.index("--base") + 1]
            return 0, ""
        if args[:2] == ["pr", "edit"] and "--body-file" in args:
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            self.prs[int(args[2])]["body"] = body
            self.bodies.append(body)
            return 0, ""
        if args[:2] == ["pr", "update-branch"]:
            merge_into(self.hub, self.prs[int(args[2])]["headRefName"], "main")
            return 0, ""
        if args[:2] == ["pr", "ready"]:
            self.prs[int(args[2])]["isDraft"] = False
            return 0, ""
        if args[:2] == ["release", "view"]:
            tag = args[2]
            if tag not in self.releases:
                return 1, "release not found"
            return 0, json.dumps({"tagName": tag, "name": f"SCIO {tag}", "url": self.releases[tag]})
        return 1, f"no fake for gh {' '.join(args)}"


def _watched(runner: FakeRunner) -> list[list[str]]:
    return [c[1:] for c in runner.calls if len(c) > 1 and c[1].endswith("watch_ci.py")]


def _python(watch_code: int = 0, facts: str = "# Session state\n\n- facts\n") -> Any:
    def python(argv: list[str], cwd: Path | None) -> tuple[int, str]:
        if argv[0].endswith("watch_ci.py"):
            return watch_code, ""
        if argv[0].endswith("session_state.py"):
            return 0, facts
        return 1, f"unexpected python {argv}"

    return python


def _stacked(tmp_path: Path) -> tuple[Path, Path, Path, FakeGitHub]:
    """#7 (feat/x) merged into main; #8 (feat/y, stacked on feat/x) is a draft whose head was
    gated in its worktree before #7 merged."""
    hub = make_hub(tmp_path)
    branch_commit(hub, "feat/x", {"x.py": "x = 1\n"})
    branch_commit(hub, "feat/y", {"y.py": "y = 1\n"}, start="feat/x")
    merge_into(hub, "main", "feat/x")
    scio = clone(hub, tmp_path / "SCIO")
    wt = tmp_path / "wt-y"
    git(scio, "worktree", "add", "-q", "-b", "feat/y", str(wt), "origin/feat/y")
    stamp(wt, tree(wt), mode="quick", head=git(wt, "rev-parse", "HEAD"))
    gh = FakeGitHub(
        hub,
        {
            8: {
                "number": 8,
                "title": "feat: y",
                "headRefName": "feat/y",
                "baseRefName": "feat/x",
                "isDraft": True,
                "body": TEMPLATE_BODY,
            }
        },
    )
    gh.merged_heads["feat/x"] = 7
    return hub, scio, wt, gh


def test_land_retargets_updates_checks_the_stamp_and_watches_by_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub, scio, wt, gh = _stacked(tmp_path)
    gated_tree = tree(wt)
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["8", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    calls = runner.gh_calls()
    assert ["pr", "edit", "8", "--base", "main"] in calls
    assert ["pr", "update-branch", "8"] in calls
    new_head = git(hub, "rev-parse", "feat/y")
    assert git(hub, "rev-parse", "feat/y^{tree}") == gated_tree, "main's merge adds nothing"
    assert "retargeted #8 from feat/x to main" in out
    assert f"updated #8 with main: {new_head[:12]}" in out
    # CI is watched for the new head's own commit, never a branch (I-211).
    [watch] = _watched(runner)
    assert watch[watch.index("--sha") + 1] == new_head and "--branch" not in watch
    # Ready only after CI passed; the merge is the owner's.
    order = runner.events
    assert order.index("gh pr ready") > order.index("python watch_ci.py")
    assert gh.prs[8]["isDraft"] is False
    assert not any(c[:2] == ["pr", "merge"] for c in calls)
    assert out.rstrip().endswith('waiting for the owner\'s "merge 8"')


def test_land_stops_when_the_update_changed_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub, scio, _wt, gh = _stacked(tmp_path)
    commit(hub, {"docs/other.md": "main moved on\n"}, "another change on main")
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["8", "--root", str(scio)], runner=runner) == 1
    out = capsys.readouterr().out
    assert "re-gate: the update changed content" in out
    assert _watched(runner) == [] and ["pr", "ready", "8"] not in runner.gh_calls()


def test_land_stops_when_the_head_was_never_gated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub = make_hub(tmp_path)
    branch_commit(hub, "feat/z", {"z.py": "z = 1\n"})
    scio = clone(hub, tmp_path / "SCIO")
    gh = FakeGitHub(
        hub, {9: {"number": 9, "headRefName": "feat/z", "baseRefName": "main", "body": ""}}
    )
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["9", "--root", str(scio)], runner=runner) == 1
    assert "the head was never gated" in capsys.readouterr().out
    assert _watched(runner) == []


def _gated_pr(
    tmp_path: Path, body: str = TEMPLATE_BODY, **stamp_fields: Any
) -> tuple[Path, Path, Path, FakeGitHub]:
    """#9 (feat/z) is up to date with main and its head is gated in its worktree."""
    hub = make_hub(tmp_path)
    branch_commit(hub, "feat/z", {"z.py": "z = 1\n"})
    scio = clone(hub, tmp_path / "SCIO")
    wt = tmp_path / "wt-z"
    git(scio, "worktree", "add", "-q", "-b", "feat/z", str(wt), "origin/feat/z")
    stamp(wt, tree(wt), head=git(wt, "rev-parse", "HEAD"), **stamp_fields)
    gh = FakeGitHub(
        hub,
        {
            9: {
                "number": 9,
                "title": "feat: z",
                "headRefName": "feat/z",
                "baseRefName": "main",
                "isDraft": False,
                "body": body,
            }
        },
    )
    return hub, scio, wt, gh


def test_land_prints_the_merge_only_for_an_approved_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    _hub, scio, _wt, gh = _gated_pr(tmp_path, body="Closes #90\n\nRefs #122\n")
    approvals = scio / ".scio" / "approvals.md"

    def landed() -> str:
        runner = FakeRunner(gh=gh, python=_python())
        assert land.main(["9", "--root", str(scio)], runner=runner) == 0
        assert not any(c[:2] == ["pr", "merge"] for c in runner.gh_calls()), "never merges"
        return capsys.readouterr().out

    assert 'waiting for the owner\'s "merge 9"' in landed()  # no approvals file at all
    write(
        scio,
        {
            ".scio/approvals.md": "# Approvals\n\n"
            "2026-11-14T12:00Z | merge 19, not PR 9 yet | PR 19, PR 90\n"
            "2026-11-14T12:05Z | merge the #12 work | Refs #12, issue 9\n"
        },
    )
    out = landed()
    assert 'waiting for the owner\'s "merge 9"' in out and "gh pr merge" not in out
    with approvals.open("a", encoding="utf-8", newline="\n") as f:
        f.write("2026-11-14T12:10Z | mege for me: the #90 work | issue 90\n")
    out = landed()
    assert "approved by the owner: gh pr merge 9 --merge" in out
    assert "mege for me: the #90 work" in out

    # Each way an approval names a PR, and the near misses.
    found = land.read_approvals(
        "x | words | PR 9\n"
        "- y | words naming PR 9 | issue 90\n"
        "| z | words | Refs #122 |\n"
        "not an approval line\n"
    )
    assert [a.refs for a in found] == [[("pr", 9)], [("issue", 90)], [("refs", 122)]]
    assert land.approval_for(9, "", found[:1]) is found[0]
    assert land.approval_for(12, "", found[:1]) is None
    assert land.approval_for(5, "Closes #90", found[1:2]) is found[1]
    assert land.approval_for(5, "Closes #901", found[1:2]) is None
    assert land.approval_for(5, "Refs #122 and more", found[2:]) is found[2]
    assert land.approval_for(5, "Refs #1222", found[2:]) is None
    assert land.approval_for(5, "Closes #122", found[2:]) is None


def test_land_writes_the_pr_body_from_the_stamp(tmp_path: Path) -> None:
    land = load("land")
    counts = {"pytest": "2345 passed, 12 skipped", "evals": "pass^5 1.00"}
    _hub, scio, wt, gh = _gated_pr(tmp_path, mode="full", counts=counts, at="2026-11-14T12:34:56Z")
    write(
        scio,
        {
            ".scio/agents/90.md": "# Agent report: issue 90\n\n## Summary\n"
            "The session tools land a PR from one command.\n\n## Files\n- scripts/land.py\n"
        },
    )
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["9", "--body", "--root", str(scio)], runner=runner) == 0
    [body] = gh.bodies
    head = git(wt, "rev-parse", "HEAD")
    assert "\r" not in body
    evidence = land.section(body, "## Test evidence")
    assert "passed in full mode at 2026-11-14T12:34:56Z" in evidence
    assert f"head: `{head[:12]}`, tree `{tree(wt)[:12]}`" in evidence
    assert "pytest 2345 passed, 12 skipped, evals pass^5 1.00" in evidence
    assert "<command>" not in body
    summary = land.section(body, "## Summary")
    assert "The session tools land a PR from one command." in summary
    assert "Closes #90" in summary, "the issue link stays"
    assert "## Docs updated\n- [x] N/A" in body, "the rest of the body is untouched"
    assert _watched(runner) == [], "--body only writes the body"

    # Run again: the body is current, nothing is written.
    again = FakeRunner(gh=gh, python=_python())
    assert land.main(["9", "--body", "--root", str(scio)], runner=again) == 0
    assert len(gh.bodies) == 1


def _merged_stack(tmp_path: Path) -> tuple[Path, Path, FakeGitHub]:
    """#7 (feat/x, touching scio/) is merged on GitHub; #8 (feat/y) is based on feat/x; the
    main checkout has not pulled yet."""
    hub = make_hub(tmp_path)
    scio = clone(hub, tmp_path / "SCIO")
    branch_commit(hub, "feat/x", {"scio/x.py": "x = 1\n"})
    branch_commit(hub, "feat/y", {"y.py": "y = 1\n"}, start="feat/x")
    merge = merge_into(hub, "main", "feat/x")
    gh = FakeGitHub(
        hub,
        {
            7: {
                "number": 7,
                "title": "feat: x",
                "headRefName": "feat/x",
                "baseRefName": "main",
                "state": "MERGED",
                "mergedAt": None,
                "mergeCommit": {"oid": merge},
                "files": [{"path": "scio/x.py"}, {"path": "docs/x.md"}],
            },
            8: {"number": 8, "title": "feat: y", "headRefName": "feat/y", "baseRefName": "feat/x"},
        },
    )
    return hub, scio, gh


def test_after_merge_checks_merged_at_pulls_and_retargets_dependents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub, scio, gh = _merged_stack(tmp_path)
    write(scio, {".scio/queue": "feat/x\nfeat/q\n"})
    before = git(scio, "rev-parse", "HEAD")

    # Not merged yet: the mergedAt read is the only step.
    runner = FakeRunner(gh=gh, python=_python())
    assert land.main(["7", "--after-merge", "--root", str(scio)], runner=runner) == 1
    assert runner.gh_calls() == [["pr", "view", "7", "--json", "mergedAt"]]
    assert "not merged (mergedAt is null)" in capsys.readouterr().out
    assert git(scio, "rev-parse", "HEAD") == before

    gh.prs[7]["mergedAt"] = "2026-11-14T13:00:00Z"
    runner = FakeRunner(gh=gh, python=_python(facts="# Session state\n\n- regenerated\n"))
    assert land.main(["7", "--after-merge", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    calls = runner.gh_calls()
    assert calls[0] == ["pr", "view", "7", "--json", "mergedAt"], "mergedAt, in its own call"
    assert git(scio, "rev-parse", "HEAD") == git(hub, "rev-parse", "main"), "pulled"
    assert ["pr", "edit", "8", "--base", "main"] in calls
    assert gh.prs[8]["baseRefName"] == "main"
    assert "retargeted #8 (feat/y) to main; requeued" in out
    queue = (scio / ".scio" / "queue").read_text(encoding="utf-8")
    assert queue == "feat/q\nfeat/y\n", "the merged branch leaves the queue, its dependent joins"
    assert "move_live_wt.py --when-idle" in out and "(scio)" in out
    facts = (scio / ".scio" / "session-state.md").read_text(encoding="utf-8")
    assert facts == "# Session state\n\n- regenerated\n"
    assert runner.pushes == [], "not a release: no tag"


def test_landing_a_release_pr_tags_its_merge_and_checks_the_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub = make_hub(tmp_path)
    scio = clone(hub, tmp_path / "SCIO")
    git(scio, "config", "user.name", "owner")  # the owner's checkout has an identity
    git(scio, "config", "user.email", "owner@example.com")
    branch_commit(hub, "release/0.11.0", {"CHANGELOG.md": "## [0.11.0]\n"})
    merge = merge_into(hub, "main", "release/0.11.0")
    gh = FakeGitHub(
        hub,
        {
            30: {
                "number": 30,
                "title": "chore(release): 0.11.0",
                "headRefName": "release/0.11.0",
                "baseRefName": "main",
                "state": "MERGED",
                "mergedAt": "2026-11-14T14:00:00Z",
                "mergeCommit": {"oid": merge},
                "files": [{"path": "CHANGELOG.md"}],
            }
        },
    )
    gh.releases["v0.11.0"] = "https://github.com/o/r/releases/tag/v0.11.0"
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["30", "--after-merge", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    assert git(scio, "cat-file", "-t", "v0.11.0") == "tag", "annotated"
    assert git(scio, "rev-parse", "v0.11.0^{commit}") == merge, "on the merge commit"
    [push] = runner.pushes
    assert push[3:] == ["push", "origin", "v0.11.0"] and same(push[2], scio)
    [watch] = _watched(runner)
    assert watch[watch.index("--sha") + 1] == merge
    assert watch[watch.index("--workflow") + 1] == "release"
    order = runner.events
    assert order.index("git push") < order.index("python watch_ci.py")
    assert order.index("python watch_ci.py") < order.index("gh release view")
    assert "release v0.11.0: https://github.com/o/r/releases/tag/v0.11.0" in out
    assert "move_live_wt" not in out, "a docs-only release moves nothing"

    # Run again: the tag already sits on the merge; it is kept, pushed and checked again.
    again = FakeRunner(gh=gh, python=_python())
    assert land.main(["30", "--after-merge", "--root", str(scio)], runner=again) == 0
    assert "tagged" not in capsys.readouterr().out


def test_land_next_starts_the_next_branchs_gate_while_ci_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    land = load("land")
    hub, scio, _wt, gh = _gated_pr(tmp_path)
    branch_commit(hub, "feat/v", {"v.py": "v = 1\n"})
    git(scio, "fetch", "-q", "origin")
    wt_v = tmp_path / "wt-v"
    git(scio, "worktree", "add", "-q", "-b", "feat/v", str(wt_v), "origin/feat/v")
    write(scio, {".scio/queue": "feat/z\nfeat/w\nfeat/v\n"})  # feat/w has no worktree here
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["9", "--next", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    [(argv, cwd, log, env)] = runner.spawned
    assert argv[-1] == "scripts/gate.py" and same(cwd, wt_v)
    assert same(log, wt_v / ".scio" / "gate" / "next.log")
    assert env is not None and env["UV_SYSTEM_CERTS"] == "1"
    order = runner.events
    assert order.index("spawn") < order.index("python watch_ci.py"), "gated while CI runs"
    assert "next: feat/w has no worktree here; skipped" in out
    assert "next: gating feat/v" in out
