"""Land a PR up to the merge, and finish the tail after it (issue #122, W-22 to W-26, W-29;
issue #141).

    uv run python scripts/land.py N                # land N up to its merge line
    uv run python scripts/land.py N --next         # ... and gate the next queued branch meanwhile
    uv run python scripts/land.py N --body         # only: write the PR body from the gate stamp
    uv run python scripts/land.py N --enqueue      # the merge line: add N to the merge queue
    uv run python scripts/land.py N --after-merge  # after N merged: the tail, then main's CI
    uv run python scripts/land.py N --after-merge --no-watch-main  # ... without main's CI

Landing N, in this order:
1. read the PR (``gh pr view``), and fetch ``origin``;
2. when its base is another branch whose own PR is merged, retarget it to ``main``;
3. when GitHub reports it conflicting (``mergeable`` CONFLICTING or ``mergeStateStatus``
   DIRTY), it stops and says so. A branch behind ``main`` is not updated: the ruleset no
   longer requires it (#141), and main's own CI after the merge is the check;
4. a gate stamp (``.scio/gate/last-pass.json`` in any worktree) must hold the head's tree,
   or it stops: "the head was never gated";
5. with ``--next``, it starts ``scripts/gate.py`` in the worktree of the next branch in
   ``.scio/queue``, in the background, so that gate runs while this PR's CI does;
6. it watches CI for the head's commit (``watch_ci.py --sha``, found by commit, I-211);
7. a draft is marked ready, only after CI passed;
8. it prints the merge line, first match wins: ``.scio/approvals.md`` approves N; N is a
   fast revert (title ``revert:``, body ``Reverts #M``, M merged, only M's files), labelled
   ``revert``; N is low risk (``risk_of``: docs and tests only), labelled ``low-risk``.
   Otherwise it waits for the owner and says which paths need the review. It never merges.
   ``main`` requires a merge queue: the line is ``land.py N --enqueue``, and the queue merges
   N after its own ``merge_group`` CI run passes.

``--enqueue`` re-checks that basis (never a high-risk PR without an approval) and CI on the
head, then adds N to the queue with GraphQL's ``enqueuePullRequest``, pinned to that head.
Never ``gh pr merge``: under the queue it goes through auto-merge, which this repository
refuses (I-233). A PR already queued is left as it is.

``--after-merge`` checks ``mergedAt`` in its own call first (I-087). While N is still in the
merge queue (GraphQL ``isInMergeQueue``) it polls until the queue merges it, for
``--queue-timeout`` minutes (30); an open PR out of the queue stops it. Then it pulls the main
checkout, retargets the PRs based on N's branch to ``main`` and requeues them, says when
live-wt should move (N touched runtime code), tags a ``chore(release): X.Y.Z`` PR's merge
and checks its release, and regenerates ``.scio/session-state.md``. Then it watches main's
``ci`` run for N's merge commit, by its SHA. When that run failed, it opens one ``red-main``
issue (``main is red after #N``, with the run's URL), prints the revert recipe, and exits 1.

The shared files live in the main checkout's ``.scio/``: ``approvals.md`` (one line per
approval, ``<UTC time> | <the owner's words> | <refs>``, refs being ``PR <n>``, ``issue <n>``
or ``Refs #<n>``), ``queue`` (one branch per line) and ``agents/<issue>.md`` (an agent's
report). Exit 0 when the step it was asked for is done, 1 when a step refused, 2 on bad use.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
PR_FIELDS = (
    "number,id,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,isDraft,body,title,"
    "state,labels"
)
AFTER_FIELDS = "number,title,headRefName,baseRefName,mergeCommit,files"
RUN_FIELDS = "databaseId,headSha,headBranch,event,status,conclusion,url"
LOW_RISK_DIRS = ("docs/", "changes/", "tests/")
"""Low risk (#141): every changed path is under these, or is Markdown outside the next list."""
HIGH_RISK_DIRS = (
    ".claude/",
    ".github/",
    "scio/",
    "scripts/",
    "recovery/",
    "evals/",
    "configs/",
    "prompts/",
)
"""Never low, Markdown or not: the agents' settings and CI, the runtime and its prompt
templates (``scio/prompt/templates/**.md``), the tools and hooks, the evals' fixtures."""
LOW_RISK, REVERT, RED_MAIN = "low-risk", "revert", "red-main"
LABELS = {
    LOW_RISK: ("0E8A16", "Docs/tests only: merged when green without the owner's review (#141)"),
    REVERT: ("FBCA04", "A fast revert of a merge that turned main red (#141)"),
    RED_MAIN: ("B60205", "main's CI failed after a merge: revert it (#141)"),
}
"""The labels land.py puts on GitHub, made with ``gh label create`` when missing, never reset."""
RED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
ENQUEUES = "# adds it to the merge queue"
"""A shell comment, so the merge line can be pasted as it is."""
ENQUEUE_MUTATION = (
    "mutation($id: ID!, $head: GitObjectID!) "
    "{ enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) "
    "{ mergeQueueEntry { position state } } }"
)
"""Adds a PR to ``main``'s merge queue; GitHub refuses it when the head moved off ``$head``.
Not ``gh pr merge``: under the queue it calls ``enablePullRequestAutoMerge``, refused here
(``allow_auto_merge`` is false, I-233). Never ``--admin``: it bypasses the queue."""
QUEUE_TIMEOUT_MIN = 30.0
QUEUE_POLL_S = 30.0
QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) "
    "{ repository(owner: $owner, name: $name) { pullRequest(number: $number) "
    "{ isInMergeQueue } } }"
)
"""Whether a PR is queued: GraphQL only, no ``gh pr view --json`` field has it."""
REVERT_TITLE_RE = re.compile(r"^revert:", re.IGNORECASE)
REVERTS_RE = re.compile(r"\breverts\s+#(\d+)\b", re.IGNORECASE)
ISSUE_URL_RE = re.compile(r"/issues/(\d+)\b")
RUNTIME_PREFIXES = ("scio/", "configs/", "scripts/hooks/", "prompts/")
"""What live-wt runs: a merge that touched these is not live until live-wt moves (W-27)."""
RELEASE_RE = re.compile(r"^chore\(release\):\s*v?(\d+\.\d+\.\d+)\s*$")
APPROVALS = Path(".scio") / "approvals.md"
QUEUE = Path(".scio") / "queue"
AGENTS = Path(".scio") / "agents"
EVIDENCE = "## Test evidence"
SUMMARY = "## Summary"
GENERATED = "<!-- written by scripts/land.py --body from .scio/gate/last-pass.json -->"
CLOSING_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", re.IGNORECASE)
LINK_LINE_RE = re.compile(r"^\s*(?:closes|fixes|resolves|refs|part of)\s+#\d+", re.IGNORECASE)
BRANCH_ISSUE_RE = re.compile(r"^[\w.-]+/(\d+)-")
REF_RE = re.compile(r"^(PR|issue|Refs)\s*#?\s*(\d+)$", re.IGNORECASE)


def _session_state() -> ModuleType:
    """``session_state.py`` beside this file: the runner, worktrees and stamps it shares.
    Loaded by path, as ``session_state.sibling`` loads (the same ``sys.modules`` key)."""
    path = SCRIPTS / "session_state.py"
    key = f"_session_tools_session_state_{abs(hash(str(path)))}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod  # @dataclass looks its module up there (I-174)
        spec.loader.exec_module(mod)
    return sys.modules[key]


@dataclass
class Basis:
    """Why a PR may merge (#141): ``kind`` is ``approved``, ``revert``, ``low`` or ``high``
    (no basis); ``risk`` is ``risk_of`` its paths, whatever the kind."""

    kind: str
    detail: str
    risk: str


class Stop(Exception):
    """A step refused: the message says why and what to do; ``code`` is the exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# Approvals (W-23).


@dataclass
class Approval:
    at: str
    words: str
    refs: list[tuple[str, int]]


def read_approvals(text: str) -> list[Approval]:
    """The lines ``<UTC time> | <the owner's words> | <refs>``. Only the last field holds
    refs: the owner's words may say "PR 5" without approving it. A Markdown bullet or a
    table row's outer pipes are accepted."""
    found: list[Approval] = []
    for raw in text.splitlines():
        line = raw.strip().removeprefix("- ").removeprefix("* ").strip().strip("|").strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or line.startswith("#"):
            continue
        refs: list[tuple[str, int]] = []
        for ref in parts[-1].split(","):
            m = REF_RE.match(ref.strip())
            if m:
                refs.append((m.group(1).lower(), int(m.group(2))))
        if refs:
            found.append(Approval(parts[0], " | ".join(parts[1:-1]), refs))
    return found


def approval_for(number: int, body: str, approvals: list[Approval]) -> Approval | None:
    """The approval that covers PR ``number``: one naming ``PR <number>``, or ``issue <m>``
    when the PR says ``Closes #<m>``, or ``Refs #<m>`` when its body has ``Refs #<m>``."""
    for a in approvals:
        for kind, n in a.refs:
            if kind == "pr" and n == number:
                return a
            if kind == "issue" and re.search(rf"\bcloses\s+#{n}\b", body, re.IGNORECASE):
                return a
            if kind == "refs" and re.search(rf"\brefs\s+#{n}\b", body, re.IGNORECASE):
                return a
    return None


# Risk (#141): "Auto-merge low-risk PRs when green (docs-only, tests-only), with your review
# reserved for scio/ runtime and hooks."


def _low_risk(path: str) -> bool:
    path = path.replace("\\", "/").removeprefix("./")
    if path.rsplit("/", 1)[-1].lower() == "claude.md" or path.startswith(HIGH_RISK_DIRS):
        return False
    return path.startswith(LOW_RISK_DIRS) or path.endswith(".md")


def high_risk_paths(paths: Iterable[str]) -> list[str]:
    """The changed paths that need the owner's review."""
    return [p for p in paths if not _low_risk(p)]


def risk_of(paths: Iterable[str]) -> Literal["low", "high"]:
    """``low`` when every changed path is under ``docs/``, ``changes/`` or ``tests/``, or is a
    ``*.md`` file, except a ``CLAUDE.md`` at any depth and anything under ``HIGH_RISK_DIRS``
    (the runtime's Markdown prompts and the evals' fixtures are code). Everything else is
    ``high``, and so is an empty change list: there is nothing to vouch for."""
    paths = list(paths)
    return "low" if paths and not high_risk_paths(paths) else "high"


# The PR body (W-24).


def replace_section(body: str, heading: str, content: str) -> str:
    """``body`` with the section under ``heading`` (to the next ``## `` heading) replaced by
    ``content``; appended when the section is missing. The same input gives the same output."""
    block = f"{heading}\n{content.strip()}\n\n"
    pattern = re.compile(rf"^{re.escape(heading)}[ \t]*\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    if pattern.search(body):
        new = pattern.sub(lambda _m: block, body, count=1)
    else:
        new = f"{body.rstrip()}\n\n{block}" if body.strip() else block
    return new.rstrip() + "\n"


def section(body: str, heading: str) -> str | None:
    m = re.search(rf"^{re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def report_summary(report: str) -> str:
    """An agent report's summary: its ``Summary`` section (any heading level), or a
    ``summary:`` field, or else its first paragraph that is not a heading."""
    m = re.search(
        r"^#{1,6}[ \t]*Summary[ \t]*\n(.*?)(?=^#{1,6}[ \t]|\Z)",
        report,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"^[-*]?[ \t]*\**summary\**[ \t]*:\**[ \t]*(\S.*)$", report, re.M | re.I)
    if m:
        return m.group(1).strip()
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", report)]
    return next((p for p in paragraphs if p and not p.startswith("#")), "")


def evidence_block(stamp: dict[str, Any], head: str, tree: str) -> str:
    counts = stamp.get("counts") or {}
    if isinstance(counts, dict) and counts:
        shown = ", ".join(
            f"{k} {json.dumps(v) if isinstance(v, dict | list) else v}" for k, v in counts.items()
        )
    else:
        shown = "none recorded"
    gated_head = str(stamp.get("head") or "")
    same = "" if not gated_head or gated_head == head else f", gated at `{gated_head[:12]}`"
    return "\n".join(
        [
            GENERATED,
            f"`uv run python scripts/gate.py` passed in {stamp.get('mode', '?')} mode at "
            f"{stamp.get('at', '?')}.",
            "",
            f"- head: `{head[:12]}`, tree `{tree[:12]}`{same}",
            f"- counts: {shown}",
        ]
    )


def issue_number(pr: dict[str, Any]) -> int | None:
    """The PR's issue: its first ``Closes #N``, or the ``<type>/<N>-slug`` branch."""
    m = CLOSING_RE.search(str(pr.get("body") or ""))
    if m:
        return int(m.group(1))
    m = BRANCH_ISSUE_RE.match(str(pr.get("headRefName") or ""))
    return int(m.group(1)) if m else None


def render_body(body: str, stamp: dict[str, Any], head: str, tree: str, report: str | None) -> str:
    """The PR body with its Test evidence block from the stamp, and its Summary from the
    agent's report when there is one. The ``Closes``/``Refs`` lines of the summary stay."""
    new = body.replace("\r\n", "\n")
    if report:
        summary = report_summary(report)
        if summary:
            old = section(new, SUMMARY) or ""
            links = [ln.strip() for ln in old.splitlines() if LINK_LINE_RE.match(ln)]
            content = summary + ("\n\n" + "\n".join(links) if links else "")
            new = replace_section(new, SUMMARY, content)
    return replace_section(new, EVIDENCE, evidence_block(stamp, head, tree))


# The queue (W-25, W-29).


def read_queue(main: Path) -> list[str]:
    try:
        lines = (main / QUEUE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def write_queue(main: Path, branches: list[str]) -> None:
    path = main / QUEUE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("".join(f"{b}\n" for b in branches))


class Lander:
    def __init__(self, root: Path, runner: Any, ss: ModuleType) -> None:
        self.root = root
        self.runner = runner
        self.ss = ss
        self.main = ss.main_checkout(root, runner)

    # Plumbing.

    def gh(self, *args: str) -> str:
        proc = self.runner.run(["gh", *args], cwd=self.root, timeout=300)
        if proc.returncode != 0:
            why = (proc.stderr or proc.stdout).strip()
            raise Stop(f"gh {' '.join(args[:3])} failed (exit {proc.returncode}): {why}")
        return str(proc.stdout)

    def gh_json(self, *args: str) -> Any:
        out = self.gh(*args)
        try:
            return json.loads(out or "null")
        except ValueError as exc:
            raise Stop(f"gh {' '.join(args[:3])} printed no JSON: {out[:200]}") from exc

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = self.runner.run(["git", "-C", str(cwd or self.root), *args], timeout=300)
        if check and proc.returncode != 0:
            why = (proc.stderr or proc.stdout).strip()
            raise Stop(f"git {' '.join(args[:2])} failed (exit {proc.returncode}): {why}")
        return str(proc.stdout).strip()

    def view(self, n: int) -> dict[str, Any]:
        data = self.gh_json("pr", "view", str(n), "--json", PR_FIELDS)
        if not isinstance(data, dict):
            raise Stop(f"gh pr view {n} gave no PR")
        return data

    def fetch(self) -> None:
        self.git("fetch", "--quiet", "origin")

    def tree(self, rev: str) -> str:
        tree = self.ss.tree_of(self.root, self.runner, rev)
        if not tree:
            raise Stop(f"{rev[:12]} is not in this clone even after a fetch")
        return str(tree)

    def gated(self, tree: str) -> tuple[Path, dict[str, Any]] | None:
        found = self.ss.find_stamp(self.root, self.runner, tree)
        return found if found else None

    def changed_paths(self, head: str) -> list[str]:
        """The PR's changed paths as GitHub's diff has them, from its merge base with
        ``origin/main`` to ``head``. ``--no-renames`` lists a rename's old path too: moving
        runtime code into ``docs/`` is not a docs-only change."""
        out = self.git("diff", "--name-only", "--no-renames", "-z", f"origin/main...{head}")
        return [p for p in out.split("\0") if p.strip()]

    def label(self, n: int, name: str) -> None:
        """Put ``name`` on PR ``n``, making the label first when GitHub lacks it."""
        self.make_label(name)
        self.gh("pr", "edit", str(n), "--add-label", name)
        print(f"#{n} labelled {name}")

    def make_label(self, name: str) -> None:
        """Create the label when GitHub lacks it. An existing label is left as it is, so a
        colour or description someone set on GitHub is never reset."""
        color, about = LABELS[name]
        try:
            self.gh("label", "create", name, "--color", color, "--description", about)
        except Stop as exc:
            if "already exists" not in str(exc):
                raise

    def watch(self, sha: str, workflow: str = "ci", branch: str | None = None) -> int:
        """``watch_ci.py`` for this exact commit, by commit (never the cwd's branch, I-211).
        ``branch`` only for main's own run: the merge queue's ``merge_group`` run tests the
        same commit on a ``gh-readonly-queue/`` branch and must not stand in for it."""
        argv = [sys.executable, str(SCRIPTS / "watch_ci.py"), "--sha", sha, "--workflow", workflow]
        argv += ["--branch", branch] if branch else []
        return int(self.runner.run(argv, cwd=self.root, timeout=None, stream=True).returncode)

    # Landing (W-22, W-23, W-29).

    def land(self, n: int, *, next_gate: bool = False) -> int:
        pr = self.view(n)
        if pr.get("state") != "OPEN":
            raise Stop(f"PR {n} is {pr.get('state')}; nothing to land")
        self.fetch()
        base = str(pr["baseRefName"])
        if base != "main":
            merged = self.gh_json(
                "pr",
                "list",
                "--head",
                base,
                "--state",
                "merged",
                "--json",
                "number",
                "--limit",
                "1",
            )
            if not merged:
                raise Stop(f"#{n} is based on {base}, whose PR is not merged: land that one first")
            self.gh("pr", "edit", str(n), "--base", "main")
            print(f"retargeted #{n} from {base} to main (#{merged[0]['number']} merged)")
            pr = self.view(n)
        # No update-branch step: the ruleset no longer requires an up-to-date branch (#141).
        self.refuse_a_conflict(n, pr)
        head = str(pr["headRefOid"])
        tree = self.tree(head)
        found = self.gated(tree)
        if found is None:
            raise Stop(
                f"the head was never gated: no .scio/gate/last-pass.json holds {head[:12]}'s "
                f"tree {tree[:12]}. Run the gate in {pr['headRefName']}'s worktree"
            )
        where, stamp = found
        print(f"#{n} head {head[:12]}: gated ({stamp.get('mode', '?')}) in {where}")
        if next_gate:
            self.start_next_gate(str(pr["headRefName"]))
        code = self.watch(head)
        if code != 0:
            raise Stop(f"CI did not pass for {head[:12]} (watch_ci exit {code}); not marked ready")
        if pr.get("isDraft"):
            self.gh("pr", "ready", str(n))
            print(f"#{n} marked ready for review")
        print(self.verdict(n, pr, head))
        return 0

    def refuse_a_conflict(self, n: int, pr: dict[str, Any]) -> None:
        """Stop on a PR GitHub reports conflicting. UNKNOWN (still computing) and BEHIND go
        on: with no update step, only a real conflict stops (#141)."""
        mergeable, status = pr.get("mergeable"), pr.get("mergeStateStatus")
        if mergeable == "CONFLICTING" or status == "DIRTY":
            raise Stop(
                f"#{n} conflicts with main (mergeable {mergeable}, mergeStateStatus {status}): "
                f"merge origin/main into {pr['headRefName']} in its worktree, resolve, run the "
                "gate, push, and land again"
            )

    def basis(self, n: int, pr: dict[str, Any], head: str) -> Basis:
        """Why PR ``n`` may merge (#141), first match wins: the owner's approval, a fast
        revert, low risk. ``high`` is no basis: it waits for the owner. Reads only."""
        paths = self.changed_paths(head)
        risk = risk_of(paths)
        approval = approval_for(n, str(pr.get("body") or ""), read_approvals(self.approvals()))
        if approval is not None:
            return Basis("approved", f'({approval.at}: "{approval.words}")', risk)
        reverted = self.reverts(n, pr, paths)
        if reverted is not None:
            return Basis("revert", f"#{reverted}", risk)
        if risk == "low":
            return Basis("low", f"{len(paths)} path(s), all docs or tests", risk)
        high = high_risk_paths(paths)
        shown = ", ".join(high[:3]) + (f", and {len(high) - 3} more" if len(high) > 3 else "")
        return Basis("high", shown or "no changed paths", risk)

    def mark(self, n: int, pr: dict[str, Any], basis: Basis) -> None:
        """The labels that make the basis visible on GitHub: ``revert`` or ``low-risk`` on,
        and a ``low-risk`` label left from an earlier landing off a PR that is high risk now."""
        labels = {str(lab.get("name")) for lab in pr.get("labels") or [] if isinstance(lab, dict)}
        if basis.risk == "high" and LOW_RISK in labels:
            self.gh("pr", "edit", str(n), "--remove-label", LOW_RISK)
            print(f"#{n}: removed its {LOW_RISK} label")
        if basis.kind == "revert":
            self.label(n, REVERT)
        elif basis.kind == "low":
            self.label(n, LOW_RISK)

    def verdict(self, n: int, pr: dict[str, Any], head: str) -> str:
        """The merge line for a PR whose CI passed (#141). It names ``--enqueue``: under the
        merge queue, ``gh pr merge`` goes through auto-merge, which this repository refuses
        (I-233)."""
        basis = self.basis(n, pr, head)
        self.mark(n, pr, basis)
        line = f"uv run python scripts/land.py {n} --enqueue  {ENQUEUES}"
        if basis.kind == "approved":
            return f"approved by the owner: {line}\n  {basis.detail}"
        if basis.kind == "revert":
            return f"fast revert of {basis.detail} (the owner's rule, #141): {line}"
        if basis.kind == "low":
            print(f"#{n} changes {basis.detail}")
            return f"low risk (docs/tests only; the owner's rule, #141): {line}"
        print(f"#{n} is high risk ({basis.detail}): it waits for the owner's review")
        return f'waiting for the owner\'s "merge {n}"'

    # Enqueueing (I-233).

    def enqueue(self, n: int) -> int:
        """Add PR ``n`` to the merge queue, on the same basis ``land.py N`` printed its line
        on: never a high-risk PR without the owner's approval, and only once CI passed on the
        head. GitHub's ``enqueuePullRequest`` mutation, pinned to that head
        (``expectedHeadOid``); ``gh pr merge`` would try auto-merge, which is off here."""
        pr = self.view(n)
        if pr.get("state") != "OPEN":
            raise Stop(f"PR {n} is {pr.get('state')}; nothing to enqueue")
        if self.in_queue(n):
            print(f"#{n} is already in the merge queue; nothing to do")
            return 0
        if pr.get("baseRefName") != "main" or pr.get("isDraft"):
            what = "a draft" if pr.get("isDraft") else f"based on {pr['baseRefName']}"
            raise Stop(f"#{n} is {what}: land it first (uv run python scripts/land.py {n})")
        self.fetch()
        self.refuse_a_conflict(n, pr)
        head = str(pr["headRefOid"])
        self.tree(head)  # the head is in this clone, or a clear stop
        basis = self.basis(n, pr, head)
        if basis.kind == "high":
            raise Stop(
                f"#{n} is high risk ({basis.detail}) and .scio/approvals.md does not approve it: "
                f'not enqueued. It waits for the owner\'s "merge {n}"'
            )
        code = self.watch(head)
        if code != 0:
            raise Stop(
                f"CI did not pass for #{n}'s head {head[:12]} (watch_ci exit {code}): not enqueued"
            )
        self.mark(n, pr, basis)
        node = str(pr.get("id") or "")
        if not node:
            raise Stop(f"gh pr view {n} gave no node id")
        data = self.gh_json(
            "api",
            "graphql",
            "-f",
            f"id={node}",
            "-f",
            f"head={head}",
            "-f",
            f"query={ENQUEUE_MUTATION}",
        )
        payload = ((data or {}).get("data") or {}).get("enqueuePullRequest") or {}
        entry = payload.get("mergeQueueEntry") or {}
        if not entry:
            raise Stop(f"GitHub returned no merge-queue entry for #{n}: {json.dumps(data)[:200]}")
        print(
            f"#{n} enqueued ({basis.kind}): position {entry.get('position')}, state "
            f"{entry.get('state')}. After the queue merges it: "
            f"uv run python scripts/land.py {n} --after-merge"
        )
        return 0

    def reverts(self, n: int, pr: dict[str, Any], paths: list[str]) -> int | None:
        """The PR that ``n`` reverts under the fast-revert policy (#141): its title starts
        ``revert:``, its body says ``Reverts #M``, M is merged, and ``n`` changes only M's
        files. Otherwise None, and ``n`` is judged by its risk like any other PR."""
        m = REVERTS_RE.search(str(pr.get("body") or ""))
        if not REVERT_TITLE_RE.match(str(pr.get("title") or "")) or m is None:
            return None
        target = int(m.group(1))
        try:
            info = self.gh_json("pr", "view", str(target), "--json", "mergedAt,files")
        except Stop as why:
            print(f"#{n} says it reverts #{target}, which gh cannot read ({why}): judged by risk")
            return None
        if not isinstance(info, dict) or not info.get("mergedAt"):
            print(f"#{n} says it reverts #{target}, which is not merged: judged by its risk")
            return None
        theirs = {str(f.get("path", "")) for f in info.get("files") or [] if isinstance(f, dict)}
        extra = [p for p in paths if p not in theirs]
        if not paths or extra:
            shown = ", ".join(extra[:3]) or "nothing"
            print(f"#{n} reverts #{target} but changes {shown} beyond it: judged by its risk")
            return None
        return target

    def approvals(self) -> str:
        try:
            return Path(self.main / APPROVALS).read_text(encoding="utf-8")
        except OSError:
            return ""

    def start_next_gate(self, current: str) -> None:
        """Gate the next queued branch in its own worktree, in the background (W-29)."""
        trees = {wt.branch: wt for wt in self.ss.worktrees(self.root, self.runner) if wt.branch}
        for branch in read_queue(self.main):
            if branch == current:
                continue
            wt = trees.get(branch)
            if wt is None:
                print(f"next: {branch} has no worktree here; skipped")
                continue
            if (wt.path / self.ss.RUNNING).exists():
                print(f"next: a gate already runs in {wt.path}")
                return
            stamp = self.ss.read_stamp(wt.path)
            if stamp and stamp.get("tree") == self.ss.tree_of(self.root, self.runner, wt.head):
                print(f"next: {branch} is already gated")
                continue
            log = wt.path / ".scio" / "gate" / "next.log"
            uv = shutil.which("uv")
            argv = (
                [uv, "run", "--frozen", "--all-extras", "--group", "dev", "python"]
                if uv
                else [sys.executable]
            ) + ["scripts/gate.py"]
            env = {**os.environ, "UV_SYSTEM_CERTS": "1", "SCIO_PROVIDER_OVERRIDE": "fake"}
            pid = self.runner.spawn(argv, wt.path, log, env)
            print(f"next: gating {branch} in {wt.path} (pid {pid}); log {log}")
            return
        print("next: nothing in .scio/queue to gate")

    # The body (W-24).

    def body(self, n: int) -> int:
        pr = self.view(n)
        self.fetch()
        head = str(pr["headRefOid"])
        tree = self.tree(head)
        found = self.gated(tree)
        if found is None:
            raise Stop(f"the head was never gated: no stamp holds {head[:12]}'s tree {tree[:12]}")
        _, stamp = found
        issue = issue_number(pr)
        report_path = self.main / AGENTS / f"{issue}.md" if issue is not None else None
        report = (
            report_path.read_text(encoding="utf-8")
            if report_path is not None and report_path.is_file()
            else None
        )
        old = str(pr.get("body") or "")
        new = render_body(old, stamp, head, tree, report)
        if new.rstrip() == old.replace("\r\n", "\n").rstrip():
            print(f"#{n}: the body is current")
            return 0
        path = self.main / ".scio" / "land" / f"pr-{n}-body.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        self.gh("pr", "edit", str(n), "--body-file", str(path))
        used = f" and the summary from {report_path}" if report else ""
        print(f"#{n}: wrote the Test evidence block from the stamp{used}")
        return 0

    # After the merge (W-25, W-26).

    def after_merge(
        self, n: int, *, watch_main: bool = True, queue_timeout_s: float = QUEUE_TIMEOUT_MIN * 60
    ) -> int:
        merged_at = self.wait_merged(n, queue_timeout_s)
        print(f"#{n} merged at {merged_at}")
        pr = self.gh_json("pr", "view", str(n), "--json", AFTER_FIELDS)
        branch = self.git("branch", "--show-current", cwd=self.main)
        if branch != "main":
            raise Stop(f"the main checkout {self.main} is on {branch or 'a detached HEAD'}")
        self.git("pull", "--ff-only", "--quiet", cwd=self.main)
        print(
            f"pulled main in {self.main}: {self.git('rev-parse', '--short', 'HEAD', cwd=self.main)}"
        )
        head_branch = str(pr["headRefName"])
        dependents = self.gh_json(
            "pr",
            "list",
            "--base",
            head_branch,
            "--state",
            "open",
            "--json",
            "number,headRefName,title",
        )
        queue = [b for b in read_queue(self.main) if b != head_branch]
        for dep in dependents or []:
            self.gh("pr", "edit", str(dep["number"]), "--base", "main")
            print(f"retargeted #{dep['number']} ({dep['headRefName']}) to main; requeued")
            if dep["headRefName"] not in queue:
                queue.append(str(dep["headRefName"]))
        if queue != read_queue(self.main):
            write_queue(self.main, queue)
        if queue:
            print(f"queue: {', '.join(queue)} (land each with scripts/land.py <PR>)")
        files = [str(f.get("path", "")) for f in pr.get("files") or []]
        runtime = sorted({f.split("/")[0] for f in files if f.startswith(RUNTIME_PREFIXES)})
        if runtime:
            print(
                f"#{n} touched runtime code ({', '.join(runtime)}): move live-wt when no "
                "supervised build runs: uv run python scripts/move_live_wt.py --when-idle"
            )
        merge = (pr.get("mergeCommit") or {}).get("oid")
        m = RELEASE_RE.match(str(pr.get("title") or ""))
        if m:
            if not merge:
                raise Stop(f"#{n} is merged but GitHub gave no merge commit")
            self.release(m.group(1), str(merge))
        self.session_state()
        if watch_main:
            if not merge:
                raise Stop(f"#{n} is merged but GitHub gave no merge commit: main's CI unwatched")
            self.watch_main(n, str(merge))
        return 0

    def wait_merged(self, n: int, timeout_s: float) -> str:
        """N's ``mergedAt``, read in its own call before any other step (I-087). The merge
        line only enqueues, so while N is open and in the merge queue this polls until the
        queue merges it, or ``timeout_s`` passes. An open PR that is not queued (it left the
        queue unmerged, its ``merge_group`` run failed, or it never joined) stops, as does a
        closed one."""
        deadline = self.runner.now() + timeout_s
        while True:
            info = self.gh_json("pr", "view", str(n), "--json", "state,mergedAt")
            info = info if isinstance(info, dict) else {}
            if info.get("mergedAt"):
                return str(info["mergedAt"])
            state = info.get("state")
            if state != "OPEN" or not self.in_queue(n):
                raise Stop(
                    f"PR {n} is not merged (mergedAt is null) and not in the merge queue (state "
                    f"{state}): it left the queue unmerged or never joined it, so nothing after "
                    f"it runs. Look at its checks and the queue's run, then: uv run python "
                    f"scripts/land.py {n} --enqueue"
                )
            left = deadline - self.runner.now()
            if left <= 0:
                raise Stop(
                    f"PR {n} is still queued after {timeout_s / 60:g} min: run land.py {n} "
                    "--after-merge again once the queue merges it"
                )
            print(
                f"#{n} queued: waiting for the merge queue ({left / 60:.1f} min left)", flush=True
            )
            self.runner.sleep(min(QUEUE_POLL_S, left))

    def in_queue(self, n: int) -> bool:
        data = self.gh_json(
            "api",
            "graphql",
            "-F",
            "owner={owner}",
            "-F",
            "name={repo}",
            "-F",
            f"number={n}",
            "-f",
            f"query={QUEUE_QUERY}",
        )
        repo = ((data or {}).get("data") or {}).get("repository") or {}
        return bool((repo.get("pullRequest") or {}).get("isInMergeQueue"))

    # Main's CI after the merge, and the fast-revert policy (#141).

    def watch_main(self, n: int, merge: str) -> None:
        """Watch main's ``ci`` run for the merge commit, by its SHA. The run's own record
        decides, not the watcher's exit code alone: only a finished run that failed opens a
        ``red-main`` issue; no run, an unfinished one, or a cancelled one says so and stops."""
        code = self.watch(merge, "ci", branch="main")
        if code == 0:
            print(f"main's ci passed for #{n}'s merge {merge[:12]}")
            return
        rows = self.gh_json(
            "run",
            "list",
            "--commit",
            merge,
            "--workflow",
            "ci",
            "--limit",
            "20",
            "--json",
            RUN_FIELDS,
        )
        run = next(
            (
                r
                for r in rows or []
                if str(r.get("headSha")) == merge
                and r.get("headBranch") == "main"
                and r.get("event") == "push"
            ),
            None,
        )
        retry = f"uv run python scripts/watch_ci.py --sha {merge} --branch main"
        if run is None:
            raise Stop(
                f"no ci run for #{n}'s merge {merge[:12]} on main (watch_ci exit {code}); no "
                f"{RED_MAIN} issue opened. Watch it: {retry}"
            )
        status, conclusion, url = run.get("status"), run.get("conclusion"), str(run.get("url"))
        if status == "completed" and conclusion == "success":
            print(f"main's ci passed for #{n}'s merge {merge[:12]} (watch_ci exit {code}): {url}")
            return
        if status != "completed" or conclusion not in RED_CONCLUSIONS:
            raise Stop(
                f"main's ci run for #{n}'s merge {merge[:12]} is {status}/{conclusion}, not "
                f"judged red; no {RED_MAIN} issue opened. Look at {url}, or watch it: {retry}"
            )
        issue, issue_url = self.red_main_issue(n, merge, url, str(conclusion))
        print(f"main is red after #{n}: {url}")
        print(f"{RED_MAIN} issue: {issue_url}")
        print(self.revert_recipe(n, merge, url, issue))
        raise Stop(f"main is red after #{n}: revert it with the recipe above (#141)")

    def red_main_issue(self, n: int, merge: str, run: str, conclusion: str) -> tuple[int, str]:
        """The open ``red-main`` issue for N, opened when there is none (one per merge)."""
        title = f"main is red after #{n}"
        self.make_label(RED_MAIN)
        found = self.gh_json(
            "issue",
            "list",
            "--label",
            RED_MAIN,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,url",
        )
        for issue in found or []:
            if issue.get("title") == title:
                return int(issue["number"]), str(issue["url"])
        body = (
            f"main's `ci` run for the merge of #{n} (`{merge}`) ended `{conclusion}`: {run}\n\n"
            f"The fast-revert policy (#141): revert #{n} first, with the recipe "
            f"`scripts/land.py {n} --after-merge` printed, then fix forward on a new branch."
        )
        out = self.gh("issue", "create", "--title", title, "--label", RED_MAIN, "--body", body)
        url = next((ln.strip() for ln in out.splitlines() if ISSUE_URL_RE.search(ln)), "")
        m = ISSUE_URL_RE.search(url)
        if m is None:
            raise Stop(f"gh issue create printed no issue URL: {out.strip()[:200]}")
        return int(m.group(1)), url

    def revert_recipe(self, n: int, merge: str, run: str, issue: int) -> str:
        wt = (self.main / ".claude" / "worktrees" / f"revert-{n}").as_posix()
        body = f"Reverts #{n}: main's ci went red after it ({run}). Closes #{issue}"
        return "\n".join(
            [
                f"revert it (the fast-revert policy, #141), from {self.main.as_posix()}:",
                "  git fetch origin",
                f'  git worktree add -b revert/{n} "{wt}" origin/main',
                f'  cd "{wt}"',
                f"  git revert --no-edit -m 1 {merge}",
                "  UV_SYSTEM_CERTS=1 uv sync --frozen --all-extras --group dev  # a new worktree",
                "  UV_SYSTEM_CERTS=1 uv run python scripts/gate.py",
                f"  git push -u origin revert/{n}",
                f'  gh pr create --base main --head revert/{n} --title "revert: #{n}" '
                f'--body "{body}"',
                "  uv run python scripts/land.py <the revert PR>  # a revert gets its merge line",
            ]
        )

    def release(self, version: str, merge: str) -> None:
        """Tag the release PR's merge (annotated), push the tag, wait for release.yml's run
        for it, and check the release (W-26). An existing tag on that commit is kept."""
        tag = f"v{version}"
        proc = self.runner.run(
            [
                "git",
                "-C",
                str(self.main),
                "rev-parse",
                "-q",
                "--verify",
                f"refs/tags/{tag}^{{commit}}",
            ]
        )
        existing = proc.stdout.strip() if proc.returncode == 0 else ""
        if existing and existing != merge:
            raise Stop(f"{tag} already exists on {existing[:12]}, not on the merge {merge[:12]}")
        if not existing:
            self.git("tag", "-a", tag, merge, "-m", f"SCIO {version}", cwd=self.main)
            print(f"tagged {tag} (annotated) on {merge[:12]}")
        self.git("push", "origin", tag, cwd=self.main)
        print(f"pushed {tag}")
        code = self.watch(merge, "release")
        if code != 0:
            raise Stop(f"release.yml did not pass for {tag} (watch_ci exit {code})")
        info = self.gh_json("release", "view", tag, "--json", "tagName,name,url")
        if not isinstance(info, dict) or info.get("tagName") != tag:
            raise Stop(f"gh release view {tag} shows no release for the tag")
        print(f"release {tag}: {info.get('url')}")

    def session_state(self) -> None:
        argv = [sys.executable, str(SCRIPTS / "session_state.py"), "--root", str(self.main)]
        proc = self.runner.run([*argv, "--markdown"], cwd=self.main, timeout=300)
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"session state not regenerated (exit {proc.returncode}): {proc.stderr.strip()}")
            return
        path = self.ss.write_facts(self.main, proc.stdout)
        print(f"regenerated {path}")


def main(argv: list[str] | None = None, runner: Any = None) -> int:
    ss = _session_state()
    ss.console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("pr", type=int, help="the PR number")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--next", action="store_true", help="also gate the next queued branch")
    mode.add_argument("--body", action="store_true", help="only write the PR body")
    mode.add_argument(
        "--enqueue", action="store_true", help="add the PR to the merge queue (I-233)"
    )
    mode.add_argument("--after-merge", action="store_true", help="the tail after the merge")
    ap.add_argument(
        "--no-watch-main",
        action="store_true",
        help="with --after-merge: do not watch main's CI run for the merge commit",
    )
    ap.add_argument(
        "--queue-timeout",
        type=float,
        default=QUEUE_TIMEOUT_MIN,
        metavar="MIN",
        help=f"with --after-merge: minutes to wait for the queue (default {QUEUE_TIMEOUT_MIN:g})",
    )
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    lander = Lander(args.root, runner or ss.Runner(), ss)
    try:
        if args.after_merge:
            return lander.after_merge(
                args.pr,
                watch_main=not args.no_watch_main,
                queue_timeout_s=args.queue_timeout * 60,
            )
        if args.body:
            return lander.body(args.pr)
        if args.enqueue:
            return lander.enqueue(args.pr)
        return lander.land(args.pr, next_gate=args.next)
    except Stop as stop:
        print(f"land: {stop}", flush=True)
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
