"""`scripts/land.py` (issue #122, W-22 to W-26, W-29; issue #141): a PR is landed up to the
merge, and one command finishes the tail after it.

GitHub is the ``hub`` repository plus a fake ``gh`` that reads PR heads from it, keeps labels,
issues and CI runs, and makes merges there, as GitHub does; ``land.py``'s own ``git fetch``,
``git diff`` and ``git pull`` are real, its ``git push`` is only recorded.
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
        self.labels: dict[str, dict[str, str]] = {}
        self.issues: list[dict[str, Any]] = []
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.queue: dict[int, list[dict[str, Any]]] = {}
        """The merge queue's script: each ``pr view`` of N first applies N's next change."""

    def view(self, n: int) -> dict[str, Any]:
        pr = dict(self.prs[n])
        if pr.get("state", "OPEN") == "OPEN":
            pr["headRefOid"] = git(self.hub, "rev-parse", pr["headRefName"])
        pr.setdefault("state", "OPEN")
        pr.setdefault("labels", [])
        pr.setdefault("mergeable", "MERGEABLE")
        pr["mergeStateStatus"] = "DRAFT" if pr.get("isDraft") else pr.get("status", "CLEAN")
        return pr

    def labels_of(self, n: int) -> list[str]:
        return [str(lab["name"]) for lab in self.prs[n].get("labels", [])]

    def __call__(self, args: list[str]) -> tuple[int, str]:
        if args[:2] == ["pr", "view"] and self.queue.get(int(args[2])):
            self.prs[int(args[2])].update(self.queue[int(args[2])].pop(0))
        if args[:2] == ["api", "graphql"]:
            fields = dict(a.split("=", 1) for a in args[2:] if "=" in a and "query=" not in a)
            assert fields["owner"] == "{owner}" and fields["name"] == "{repo}", "gh fills these"
            assert "isInMergeQueue" in next(a for a in args if a.startswith("query="))
            queued = bool(self.prs[int(fields["number"])].get("isInMergeQueue"))
            pr = {"pullRequest": {"isInMergeQueue": queued}}
            return 0, json.dumps({"data": {"repository": pr}})
        if args[:2] == ["label", "create"]:
            opts = dict(zip(args[3::2], args[4::2], strict=False))
            if args[2] in self.labels and "--force" not in args:
                return 1, f"label with name {args[2]!r} already exists; use `--force`"
            self.labels[args[2]] = {k: v for k, v in opts.items() if k != "--force"}
            return 0, ""
        if args[:2] == ["pr", "edit"] and ("--add-label" in args or "--remove-label" in args):
            pr = self.prs[int(args[2])]
            names = self.labels_of(int(args[2]))
            if "--add-label" in args:
                name = args[args.index("--add-label") + 1]
                if name not in self.labels:
                    return 1, f"'{name}' not found"
                names = [*names, name] if name not in names else names
            if "--remove-label" in args:
                names = [x for x in names if x != args[args.index("--remove-label") + 1]]
            pr["labels"] = [{"name": x} for x in names]
            return 0, ""
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1] if "--label" in args else None
            found = [
                {"number": i["number"], "title": i["title"], "url": i["url"]}
                for i in self.issues
                if i["state"] == "OPEN" and (label is None or label in i["labels"])
            ]
            return 0, json.dumps(found)
        if args[:2] == ["issue", "create"]:
            labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            if any(lab not in self.labels for lab in labels):
                return 1, "could not add label: not found"
            number = 200 + len(self.issues)
            url = f"https://github.com/o/r/issues/{number}"
            self.issues.append(
                {
                    "number": number,
                    "title": args[args.index("--title") + 1],
                    "body": args[args.index("--body") + 1],
                    "labels": labels,
                    "state": "OPEN",
                    "url": url,
                }
            )
            return 0, url + "\n"
        if args[:2] == ["run", "list"] and "--commit" in args:
            sha = args[args.index("--commit") + 1]
            return 0, json.dumps(self.runs.get(sha, []))
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


def _workflow(watch: list[str]) -> str:
    return watch[watch.index("--workflow") + 1] if "--workflow" in watch else "ci"


def _python(
    watch_code: int = 0,
    facts: str = "# Session state\n\n- facts\n",
    *,
    codes: dict[str, int] | None = None,
) -> Any:
    """``watch_ci.py`` exits ``codes[workflow]``, else ``watch_code``; ``session_state.py``
    prints ``facts``."""

    def python(argv: list[str], cwd: Path | None) -> tuple[int, str]:
        if argv[0].endswith("watch_ci.py"):
            return (codes or {}).get(_workflow(argv), watch_code), ""
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
    head = git(wt, "rev-parse", "HEAD")
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["8", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    calls = runner.gh_calls()
    assert ["pr", "edit", "8", "--base", "main"] in calls
    # Behind main after the retarget, and not updated: the ruleset no longer asks (#141).
    assert not any(c[:2] == ["pr", "update-branch"] for c in calls)
    assert git(hub, "rev-parse", "feat/y") == head
    assert "retargeted #8 from feat/x to main" in out
    assert f"#8 head {head[:12]}: gated" in out
    # CI is watched for the head's own commit, never a branch (I-211).
    [watch] = _watched(runner)
    assert watch[watch.index("--sha") + 1] == head and "--branch" not in watch
    # Ready only after CI passed; the merge is the owner's.
    order = runner.events
    assert order.index("gh pr ready") > order.index("python watch_ci.py")
    assert gh.prs[8]["isDraft"] is False
    assert not any(c[:2] == ["pr", "merge"] for c in calls)
    assert out.rstrip().endswith('waiting for the owner\'s "merge 8"')


def test_land_no_longer_updates_a_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner (#141): "Drop 'branch must be up to date.'" The ruleset no longer requires
    it, so a PR behind main is landed as it is: its own gated head, its own CI run."""
    land = load("land")
    hub, scio, _wt, gh = _stacked(tmp_path)
    commit(hub, {"docs/other.md": "main moved on\n"}, "another change on main")
    head = git(hub, "rev-parse", "feat/y")
    gh.prs[8]["status"] = "BEHIND"
    gh.prs[8]["isDraft"] = False
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["8", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    calls = runner.gh_calls()
    assert not any(c[:2] == ["pr", "update-branch"] for c in calls), "no update step"
    assert git(hub, "rev-parse", "feat/y") == head, "GitHub's branch is untouched"
    [watch] = _watched(runner)
    assert watch[watch.index("--sha") + 1] == head, "CI is watched for the head as it is"
    assert "updated #8" not in out and "re-gate" not in out
    assert out.rstrip().endswith('waiting for the owner\'s "merge 8"')


def test_land_stops_on_a_conflicting_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With no update step, a PR that conflicts with main stops before CI is watched, and
    says so; ``UNKNOWN`` (GitHub still computing) and ``BEHIND`` do not stop it."""
    land = load("land")
    _hub, scio, _wt, gh = _gated_pr(tmp_path)
    for mergeable, status in (("CONFLICTING", "DIRTY"), ("UNKNOWN", "DIRTY")):
        gh.prs[9]["mergeable"], gh.prs[9]["status"] = mergeable, status
        runner = FakeRunner(gh=gh, python=_python())
        assert land.main(["9", "--root", str(scio)], runner=runner) == 1
        out = capsys.readouterr().out
        assert "#9 conflicts with main" in out and "feat/z" in out
        assert _watched(runner) == [] and not any(
            c[:2] == ["pr", "update-branch"] for c in runner.gh_calls()
        )
    for mergeable, status in (("UNKNOWN", "UNKNOWN"), ("MERGEABLE", "BEHIND")):
        gh.prs[9]["mergeable"], gh.prs[9]["status"] = mergeable, status
        runner = FakeRunner(gh=gh, python=_python())
        assert land.main(["9", "--root", str(scio)], runner=runner) == 0, (mergeable, status)
        assert "conflicts with main" not in capsys.readouterr().out


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
    tmp_path: Path,
    body: str = TEMPLATE_BODY,
    files: dict[str, str] | None = None,
    **stamp_fields: Any,
) -> tuple[Path, Path, Path, FakeGitHub]:
    """#9 (feat/z, changing ``files``: ``z.py`` by default) is up to date with main and its
    head is gated in its worktree."""
    hub = make_hub(tmp_path)
    branch_commit(hub, "feat/z", files or {"z.py": "z = 1\n"})
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
    assert runner.gh_calls() == [["pr", "view", "7", "--json", "state,mergedAt"]]
    assert "not merged (mergedAt is null)" in capsys.readouterr().out
    assert git(scio, "rev-parse", "HEAD") == before

    gh.prs[7]["mergedAt"] = "2026-11-14T13:00:00Z"
    runner = FakeRunner(gh=gh, python=_python(facts="# Session state\n\n- regenerated\n"))
    assert land.main(["7", "--after-merge", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    calls = runner.gh_calls()
    assert calls[0] == ["pr", "view", "7", "--json", "state,mergedAt"], "in its own call"
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
    [watch] = [w for w in _watched(runner) if _workflow(w) == "release"]
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


# Issue #141: low-risk PRs merge when green, a revert merges fast, and main's CI is watched.

ENQUEUE = "gh pr merge {n} --merge  # adds it to the merge queue"
LOW_RISK_LINE = "low risk (docs/tests only; the owner's rule, #141): " + ENQUEUE.format(n=9)


def test_a_docs_or_tests_only_pr_is_low_risk() -> None:
    """The owner (#141): "Auto-merge low-risk PRs when green (docs-only, tests-only)"."""
    land = load("land")
    for paths in (
        ["docs/ARCHITECTURE.md"],
        ["tests/unit/test_x.py", "tests/conftest.py"],
        ["changes/unreleased/023-x.changed.md", "docs/ledger/I-300.md", "docs/img/flow.png"],
        ["README.md", "CONTRIBUTING.md", "examples/notes.md"],
    ):
        assert land.risk_of(paths) == "low", paths
    for paths in (
        [],  # nothing to judge is never low
        ["CLAUDE.md"],
        ["docs/guide.md", "CLAUDE.md"],
        ["docs/CLAUDE.md"],  # an agent's instructions at any depth
        [".claude/settings.json"],
        [".claude/templates/research-brief.md"],
        [".github/workflows/ci.yml"],
        [".github/pull_request_template.md"],
        ["scripts/gate.py"],
        ["scripts/hooks/push_gate.py"],
        ["scio/product/build.py"],
        ["scio/prompt/templates/coder/system.md"],  # a runtime prompt, though Markdown
        ["evals/tasks/t001_add_function/repo/README.md"],  # an eval's fixture
        ["prompts/x.md"],
        ["recovery/supervisor.py"],
        ["configs/models.yaml"],
        ["pyproject.toml"],
        ["uv.lock"],
        [".gitignore"],
        ["docs/guide.md", "tests/test_x.py", "scio/x.py"],
        ["documentation/x.py"],
        ["testsuite/x.py"],
    ):
        assert land.risk_of(paths) == "high", paths


def test_a_green_low_risk_pr_gets_its_merge_line_without_an_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner (#141): "Auto-merge low-risk PRs when green". A docs/tests-only PR whose CI
    passed gets its merge line and the ``low-risk`` label (made when missing), with no line in
    ``.scio/approvals.md``. Red CI gets neither."""
    land = load("land")
    files = {
        "docs/guide.md": "# Guide\n",
        "tests/unit/test_guide.py": "def test_guide() -> None:\n    pass\n",
        "changes/unreleased/099-guide.added.md": "A guide.\n",
    }
    _hub, scio, _wt, gh = _gated_pr(tmp_path, files=files)

    red = FakeRunner(gh=gh, python=_python(watch_code=1))
    assert land.main(["9", "--root", str(scio)], runner=red) == 1
    out = capsys.readouterr().out
    assert "gh pr merge" not in out and gh.labels_of(9) == []

    runner = FakeRunner(gh=gh, python=_python())
    assert land.main(["9", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(LOW_RISK_LINE)
    assert gh.labels_of(9) == ["low-risk"] and "low-risk" in gh.labels
    order = runner.events
    assert order.index("python watch_ci.py") < order.index("gh label create"), "only when green"
    assert not any(c[:2] == ["pr", "merge"] for c in runner.gh_calls()), "the merge is a line"

    # An approval still reads as the owner's.
    write(scio, {".scio/approvals.md": "2026-11-15T09:00Z | merge 9 | PR 9\n"})
    assert land.main(["9", "--root", str(scio)], runner=FakeRunner(gh=gh, python=_python())) == 0
    assert "approved by the owner: gh pr merge 9 --merge" in capsys.readouterr().out


def test_an_existing_label_keeps_the_colour_set_on_github(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """land.py makes a label only when GitHub lacks it: a colour or description set on GitHub
    is never reset (no ``--force``)."""
    land = load("land")
    files = {"docs/guide.md": "# Guide\n"}
    _hub, scio, _wt, gh = _gated_pr(tmp_path, files=files)
    gh.labels["low-risk"] = {"--color": "000000", "--description": "set by hand"}
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["9", "--root", str(scio)], runner=runner) == 0
    assert capsys.readouterr().out.rstrip().endswith(LOW_RISK_LINE)
    assert gh.labels["low-risk"] == {"--color": "000000", "--description": "set by hand"}
    assert gh.labels_of(9) == ["low-risk"]
    assert not any("--force" in c for c in runner.gh_calls())


def test_a_high_risk_pr_still_waits_for_the_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner (#141): "your review reserved for scio/ runtime and hooks". A PR that also
    touches runtime code waits, says why, and loses a ``low-risk`` label it carried."""
    land = load("land")
    files = {"docs/guide.md": "# Guide\n", "scio/x.py": "x = 1\n"}
    _hub, scio, _wt, gh = _gated_pr(tmp_path, files=files)
    gh.labels["low-risk"] = {}
    gh.prs[9]["labels"] = [{"name": "low-risk"}]  # labelled before scio/ joined it
    runner = FakeRunner(gh=gh, python=_python())

    assert land.main(["9", "--root", str(scio)], runner=runner) == 0
    out = capsys.readouterr().out
    assert "#9 is high risk (scio/x.py): it waits for the owner's review" in out
    assert out.rstrip().endswith('waiting for the owner\'s "merge 9"')
    assert "gh pr merge" not in out
    assert ["pr", "edit", "9", "--remove-label", "low-risk"] in runner.gh_calls()
    assert gh.labels_of(9) == []


def test_a_revert_pr_gets_its_merge_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner (#141): "keep CI on main and a fast-revert policy". A ``revert: #N`` PR whose
    body says ``Reverts #N`` gets its merge line and the ``revert`` label, even for runtime
    code, when N is merged and the revert touches only N's files."""
    land = load("land")
    hub = make_hub(tmp_path)
    branch_commit(hub, "feat/x", {"scio/x.py": "x = 1\n"})
    merge = merge_into(hub, "main", "feat/x")
    git(hub, "checkout", "-q", "-b", "revert/7", "main")
    git(hub, "revert", "--no-edit", "-m", "1", merge)
    git(hub, "checkout", "-q", "main")
    scio = clone(hub, tmp_path / "SCIO")
    wt = tmp_path / "wt-revert"
    git(scio, "worktree", "add", "-q", "-b", "revert/7", str(wt), "origin/revert/7")
    stamp(wt, tree(wt), head=git(wt, "rev-parse", "HEAD"))
    run = "https://github.com/o/r/actions/runs/99"
    gh = FakeGitHub(
        hub,
        {
            7: {
                "number": 7,
                "title": "feat: x",
                "headRefName": "feat/x",
                "baseRefName": "main",
                "state": "MERGED",
                "mergedAt": "2026-11-15T10:00:00Z",
                "mergeCommit": {"oid": merge},
                "files": [{"path": "scio/x.py"}],
            },
            12: {
                "number": 12,
                "title": "revert: #7",
                "headRefName": "revert/7",
                "baseRefName": "main",
                "isDraft": False,
                "body": f"Reverts #7: main went red after it ({run}).\n\nCloses #200\n",
            },
        },
    )

    def landed() -> str:
        runner = FakeRunner(gh=gh, python=_python())
        assert land.main(["12", "--root", str(scio)], runner=runner) == 0
        assert not any(c[:2] == ["pr", "merge"] for c in runner.gh_calls())
        return capsys.readouterr().out

    out = landed()
    assert out.rstrip().endswith(
        "fast revert of #7 (the owner's rule, #141): " + ENQUEUE.format(n=12)
    )
    assert gh.labels_of(12) == ["revert"]

    # The near misses wait for the owner: no "Reverts #7", a revert of an unmerged PR, and
    # one that changes a file #7 did not.
    gh.prs[12]["labels"] = []
    for change in (
        {"body": "Undo #7.\n"},
        {"title": "fix: undo #7"},
    ):
        saved = {k: gh.prs[12][k] for k in change}
        gh.prs[12].update(change)
        assert landed().rstrip().endswith('waiting for the owner\'s "merge 12"'), change
        gh.prs[12].update(saved)
    gh.prs[7]["mergedAt"] = None
    assert landed().rstrip().endswith('waiting for the owner\'s "merge 12"')
    gh.prs[7]["mergedAt"] = "2026-11-15T10:00:00Z"
    gh.prs[7]["files"] = [{"path": "scio/other.py"}]
    assert landed().rstrip().endswith('waiting for the owner\'s "merge 12"')
    assert gh.labels_of(12) == []


def test_after_merge_watches_mains_ci_and_opens_a_red_main_issue_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner (#141): "keep CI on main and a fast-revert policy". After the merge, main's
    CI run for the merge commit is watched by its SHA; a red run opens one ``red-main`` issue
    with the run's URL, prints the revert recipe, and exits 1."""
    land = load("land")
    _hub, scio, gh = _merged_stack(tmp_path)
    gh.prs[7]["mergedAt"] = "2026-11-15T11:00:00Z"
    merge = gh.prs[7]["mergeCommit"]["oid"]
    after = ["7", "--after-merge", "--root", str(scio)]

    # Green: main's run for the merge commit is watched, by commit; no issue.
    runner = FakeRunner(gh=gh, python=_python())
    assert land.main(after, runner=runner) == 0
    [watch] = _watched(runner)
    assert watch[watch.index("--sha") + 1] == merge and _workflow(watch) == "ci"
    assert watch[watch.index("--branch") + 1] == "main", "main's run, never the queue's"
    assert "main's ci passed" in capsys.readouterr().out and gh.issues == []

    # --no-watch-main skips the watch.
    runner = FakeRunner(gh=gh, python=_python(watch_code=1))
    assert land.main([*after, "--no-watch-main"], runner=runner) == 0
    assert _watched(runner) == [] and gh.issues == []
    capsys.readouterr()

    # Red: one issue, labelled, with the run; the recipe; exit 1. The merge queue's green
    # run of the same commit, listed first, does not hide main's own red run.
    url = "https://github.com/o/r/actions/runs/4321"
    gh.runs[merge] = [
        {
            "databaseId": 4320,
            "headSha": merge,
            "headBranch": "gh-readonly-queue/main/pr-7-0123abcd",
            "event": "merge_group",
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/o/r/actions/runs/4320",
        },
        {
            "databaseId": 4321,
            "headSha": merge,
            "headBranch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
            "url": url,
        },
    ]
    runner = FakeRunner(gh=gh, python=_python(watch_code=1))
    assert land.main(after, runner=runner) == 1
    out = capsys.readouterr().out
    [issue] = gh.issues
    assert issue["title"] == "main is red after #7" and issue["labels"] == ["red-main"]
    assert url in issue["body"] and merge in issue["body"]
    assert "red-main" in gh.labels
    for step in (
        "git fetch origin",
        "git worktree add -b revert/7",
        'revert-7" origin/main',
        f"git revert --no-edit -m 1 {merge}",
        "UV_SYSTEM_CERTS=1 uv sync --frozen --all-extras --group dev",
        "UV_SYSTEM_CERTS=1 uv run python scripts/gate.py",
        "git push -u origin revert/7",
        '--title "revert: #7"',
        "Reverts #7",
        url,
        "Closes #200",
        "scripts/land.py",
    ):
        assert step in out, step
    assert issue["url"] in out

    # Run again: the open issue is reused, not doubled.
    runner = FakeRunner(gh=gh, python=_python(watch_code=1))
    assert land.main(after, runner=runner) == 1
    assert len(gh.issues) == 1 and gh.issues[0]["url"] in capsys.readouterr().out

    # The run's record decides, not the watcher alone: a cancelled run, or none at all
    # (watch_ci exit 2), opens no issue and says so; a passed run is green.
    gh.issues.clear()
    gh.runs[merge][1]["conclusion"] = "cancelled"
    runner = FakeRunner(gh=gh, python=_python(watch_code=1))
    assert land.main(after, runner=runner) == 1
    assert gh.issues == [] and "not judged red" in capsys.readouterr().out
    gh.runs[merge][1]["conclusion"] = "success"
    assert land.main(after, runner=FakeRunner(gh=gh, python=_python(watch_code=1))) == 0
    assert gh.issues == [] and "main's ci passed" in capsys.readouterr().out
    gh.runs.clear()
    runner = FakeRunner(gh=gh, python=_python(watch_code=2))
    assert land.main(after, runner=runner) == 1
    assert gh.issues == [] and "no ci run" in capsys.readouterr().out


def test_after_merge_waits_for_the_queue_to_merge_the_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` requires a merge queue: the merge line only enqueues the PR. ``--after-merge``
    waits while the PR is queued (GraphQL ``isInMergeQueue``), runs the tail once the queue
    merged it, and stops when the PR left the queue unmerged or the wait timed out."""
    land = load("land")
    hub, scio, gh = _merged_stack(tmp_path)
    before = git(scio, "rev-parse", "HEAD")
    after = ["7", "--after-merge", "--root", str(scio)]
    gh.prs[7].update(state="OPEN", mergedAt=None, isInMergeQueue=True)

    # The queue's own run failed: the PR is open and out of the queue. Nothing after it runs.
    gh.queue[7] = [{}, {"isInMergeQueue": False}]
    runner = FakeRunner(gh=gh, python=_python())
    assert land.main(after, runner=runner) == 1
    out = capsys.readouterr().out
    assert "#7 queued" in out and "not in the merge queue" in out
    assert git(scio, "rev-parse", "HEAD") == before and _watched(runner) == []

    # Still queued when the timeout ends.
    gh.prs[7]["isInMergeQueue"] = True
    runner = FakeRunner(gh=gh, python=_python())
    assert land.main([*after, "--queue-timeout", "1"], runner=runner) == 1
    assert "still queued after 1 min" in capsys.readouterr().out
    assert sum(runner.sleeps) == 60 and git(scio, "rev-parse", "HEAD") == before

    # Queued for two reads, then merged by the queue: the tail runs, then main's CI.
    merged = {"state": "MERGED", "mergedAt": "2026-11-15T12:00:00Z", "isInMergeQueue": False}
    gh.queue[7] = [{}, {}, merged]
    runner = FakeRunner(gh=gh, python=_python())
    assert land.main(after, runner=runner) == 0
    out = capsys.readouterr().out
    assert out.count("#7 queued: waiting for the merge queue") == 2
    assert runner.sleeps == [land.QUEUE_POLL_S, land.QUEUE_POLL_S]
    assert "#7 merged at 2026-11-15T12:00:00Z" in out
    assert git(scio, "rev-parse", "HEAD") == git(hub, "rev-parse", "main"), "pulled after"
    order = runner.events
    assert order.index("gh api graphql") < order.index("python watch_ci.py")
    assert "main's ci passed" in out
