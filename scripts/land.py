"""Land a PR up to the merge, and finish the tail after it (issue #122, W-22 to W-26, W-29).

    uv run python scripts/land.py N                # land N up to the owner's merge
    uv run python scripts/land.py N --next         # ... and gate the next queued branch meanwhile
    uv run python scripts/land.py N --body         # only: write the PR body from the gate stamp
    uv run python scripts/land.py N --after-merge  # after the owner merged N: the tail

Landing N, in this order:
1. read the PR (``gh pr view``), and fetch ``origin``;
2. when its base is another branch whose own PR is merged, retarget it to ``main``;
3. when it is behind ``main``, ``gh pr update-branch``. The new head's tree must equal a tree
   the gate passed, or it stops: "re-gate: the update changed content";
4. a gate stamp (``.scio/gate/last-pass.json`` in any worktree) must hold the head's tree,
   or it stops: "the head was never gated";
5. with ``--next``, it starts ``scripts/gate.py`` in the worktree of the next branch in
   ``.scio/queue``, in the background, so that gate runs while this PR's CI does;
6. it watches CI for the head's commit (``watch_ci.py --sha``, found by commit, I-211);
7. a draft is marked ready, only after CI passed;
8. it prints the merge command when ``.scio/approvals.md`` approves N, and otherwise that it
   waits for the owner. It never merges: the merge is the owner's.

``--after-merge`` checks ``mergedAt`` in its own call first (I-087), then pulls the main
checkout, retargets the PRs based on N's branch to ``main`` and requeues them, says when
live-wt should move (N touched runtime code), tags a ``chore(release): X.Y.Z`` PR's merge
and checks its release, and regenerates ``.scio/session-state.md``.

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
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
PR_FIELDS = "number,headRefName,headRefOid,baseRefName,mergeStateStatus,isDraft,body,title,state"
AFTER_FIELDS = "number,title,headRefName,baseRefName,mergeCommit,files"
RUNTIME_PREFIXES = ("scio/", "configs/", "scripts/hooks/", "prompts/")
"""What live-wt runs: a merge that touched these is not live until live-wt moves (W-27)."""
RELEASE_RE = re.compile(r"^chore\(release\):\s*v?(\d+\.\d+\.\d+)\s*$")
APPROVALS = Path(".scio") / "approvals.md"
QUEUE = Path(".scio") / "queue"
AGENTS = Path(".scio") / "agents"
EVIDENCE = "## Test evidence"
SUMMARY = "## Summary"
GENERATED = "<!-- written by scripts/land.py --body from .scio/gate/last-pass.json -->"
NEW_HEAD_TRIES = 10
NEW_HEAD_WAIT_S = 3.0
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


def merge_line(number: int, body: str, approvals_text: str) -> str:
    approval = approval_for(number, body, read_approvals(approvals_text))
    if approval is None:
        return f'waiting for the owner\'s "merge {number}"'
    return (
        f"approved by the owner: gh pr merge {number} --merge\n"
        f'  ({approval.at}: "{approval.words}")'
    )


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

    def behind(self, pr: dict[str, Any]) -> bool:
        """Whether the head lacks ``origin/main``. ``mergeStateStatus`` is only a hint: it is
        ``UNKNOWN`` right after a push and ``DRAFT`` on a draft, so git decides."""
        if pr.get("mergeStateStatus") == "BEHIND":
            return True
        proc = self.runner.run(
            [
                "git",
                "-C",
                str(self.root),
                "merge-base",
                "--is-ancestor",
                "origin/main",
                str(pr["headRefOid"]),
            ]
        )
        return bool(proc.returncode == 1)

    def watch(self, sha: str, workflow: str = "ci") -> int:
        """``watch_ci.py`` for this exact commit, by commit (never the cwd's branch, I-211)."""
        argv = [sys.executable, str(SCRIPTS / "watch_ci.py"), "--sha", sha, "--workflow", workflow]
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
        if self.behind(pr):
            old = str(pr["headRefOid"])
            gated_before = self.gated(self.tree(old))
            self.gh("pr", "update-branch", str(n))
            pr = self.new_head(n, old)
            self.fetch()
            tree = self.tree(str(pr["headRefOid"]))
            if self.gated(tree) is None:
                raise Stop(
                    "re-gate: the update changed content. Pull "
                    f"{pr['headRefName']} into its worktree, run the gate there, then land "
                    f"again{'' if gated_before else ' (its old head was never gated either)'}"
                )
            print(f"updated #{n} with main: {pr['headRefOid'][:12]}, a tree the gate passed")
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
        print(merge_line(n, str(pr.get("body") or ""), self.approvals()))
        return 0

    def new_head(self, n: int, old: str) -> dict[str, Any]:
        """The PR once GitHub has moved its head off ``old`` after an update."""
        for _ in range(NEW_HEAD_TRIES):
            pr = self.view(n)
            if pr.get("headRefOid") != old:
                return pr
            self.runner.sleep(NEW_HEAD_WAIT_S)
        raise Stop(f"#{n}'s head did not move off {old[:12]} after gh pr update-branch")

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

    def after_merge(self, n: int) -> int:
        merged = self.gh_json("pr", "view", str(n), "--json", "mergedAt")
        merged_at = merged.get("mergedAt") if isinstance(merged, dict) else None
        if not merged_at:
            raise Stop(f"PR {n} is not merged (mergedAt is null); nothing after it runs")
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
        m = RELEASE_RE.match(str(pr.get("title") or ""))
        if m:
            merge = (pr.get("mergeCommit") or {}).get("oid")
            if not merge:
                raise Stop(f"#{n} is merged but GitHub gave no merge commit")
            self.release(m.group(1), str(merge))
        self.session_state()
        return 0

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
    mode.add_argument("--after-merge", action="store_true", help="the tail after the merge")
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    lander = Lander(args.root, runner or ss.Runner(), ss)
    try:
        if args.after_merge:
            return lander.after_merge(args.pr)
        if args.body:
            return lander.body(args.pr)
        return lander.land(args.pr, next_gate=args.next)
    except Stop as stop:
        print(f"land: {stop}", flush=True)
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
