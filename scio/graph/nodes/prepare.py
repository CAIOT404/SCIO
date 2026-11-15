"""``prepare_repo``: clone the target repository into the sandbox and create the branch, at
the task's ``start_commit`` when a retried product feature carries its verified work (issue
#119), else at the base branch's head.

Idempotent under ``run_effect``: the probe checks for an existing clone on the branch.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from scio.aci.sandbox.base import Sandbox
from scio.graph.effects import run_effect
from scio.graph.nodes.common import fact, git_out, slug
from scio.graph.run_context import RunContext
from scio.state.models import FeatureItem, FeatureStatus, Role, SCIOState
from scio.toolchain import detect_toolchain, get_toolchain

Node = Callable[[SCIOState], dict[str, Any]]


def exclude_path(sb: Sandbox) -> str:
    """The repository's ``info/exclude`` as git resolves it: ``<root>/.git/info/exclude`` in a
    plain clone, the common directory's file in a worktree, where ``.git`` is a file (I-161)."""
    r = sb.exec(["git", "rev-parse", "--git-path", "info/exclude"], timeout_s=30)
    rel = r.stdout.strip().replace("\\", "/") if r.ok else ".git/info/exclude"
    is_abs = rel.startswith("/") or (len(rel) > 1 and rel[1] == ":")
    root = sb.root.replace("\\", "/").rstrip("/")
    return rel if is_abs else f"{root}/{rel}"  # forward slashes throughout (I-169)


def under_root(sb: Sandbox, path: str) -> bool:
    """True when ``path`` lies inside the sandbox root, compared with forward slashes on both
    sides and without regard to case on Windows (I-169)."""
    root = sb.root.replace("\\", "/").rstrip("/") + "/"
    p = path.replace("\\", "/")
    return p.lower().startswith(root.lower()) if os.name == "nt" else p.startswith(root)


def branch_name(state: SCIOState) -> str:
    n = state.task.issue_number
    return f"agent/{n}-{slug(state.task.title)}" if n else f"agent/{slug(state.task.title)}"


def prepare_repo(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        sb = ctx.sandbox
        branch = branch_name(state)

        def probe() -> dict[str, str] | None:
            if not sb.exists(f"{sb.root}/.git"):
                return None
            current = git_out(sb, "rev-parse", "--abbrev-ref", "HEAD")
            if current != branch:
                return None
            base = git_out(sb, "merge-base", branch, state.task.base_branch)
            return {"branch": branch, "base_sha": base or git_out(sb, "rev-parse", "HEAD")}

        def apply() -> dict[str, str]:
            if not sb.exists(f"{sb.root}/.git"):
                r = sb.exec(["git", "clone", "-q", state.task.repo_url, "."], timeout_s=300)
                if not r.ok:
                    raise RuntimeError(f"git clone failed: {r.stderr.strip()[:300]}")
            # The clone says what stack it is (M10): adopt its profile unless one was forced.
            if ctx.settings.toolchain == "auto":
                detected = detect_toolchain(sb.root, lambda rel: sb.exists(f"{sb.root}/{rel}"))
                if detected is not None:
                    ctx.adopt_toolchain(get_toolchain(detected))
            # Repo-local excludes (not committed): never let test caches into a checkpoint.
            # Git names the file: in a worktree `.git` is a file and the excludes live in the
            # common directory (I-161).
            # A worktree's file is outside the sandbox: the orchestrator that made the worktree
            # wrote it there first (`scio/product/parallel.py`), and the sandbox may not.
            excludes = exclude_path(sb)
            if under_root(sb, excludes):
                sb.write_text(excludes, ctx.toolchain.git_exclude_text())
            sb.exec(["git", "config", "user.name", "scio"], timeout_s=30)
            sb.exec(["git", "config", "user.email", "scio@users.noreply.github.com"], timeout_s=30)
            sb.exec(["git", "checkout", "-q", state.task.base_branch], timeout_s=60)
            base = git_out(sb, "rev-parse", "HEAD")
            # A retried product feature starts from its last verified commit (issue #119);
            # the base stays the base branch's head, so diffs cover the whole feature.
            start = [state.task.start_commit] if state.task.start_commit else []
            r = sb.exec(["git", "checkout", "-q", "-B", branch, *start], timeout_s=60)
            if not r.ok:
                raise RuntimeError(f"git checkout -B failed: {r.stderr.strip()[:300]}")
            return {"branch": branch, "base_sha": base}

        out = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="prepare_repo",
            intent=f"clone+branch:{branch}",
            apply=apply,
            probe=probe,
        )
        github = state.github.model_copy(
            update={"branch": out["branch"], "base_sha": out["base_sha"]}
        )
        text = f"repository ready on branch {branch} at {out['base_sha'][:12]}"
        patch: dict[str, Any] = {"github": github, "phase": "implement"}
        if state.task.start_commit:
            features, ids = carry_features(state.feature_list, state.task.carried)
            patch["feature_list"] = features
            text = (
                f"repository ready on branch {branch} at {state.task.start_commit[:12]}, on top "
                f"of {out['base_sha'][:12]}; carried as passing: {', '.join(ids) or 'none'} "
                "(issue #119)"
            )
        patch["task_ledger"] = [fact(text, source=Role.SYSTEM)]
        return patch

    return node


def carry_features(
    features: list[FeatureItem], carried: list[FeatureItem]
) -> tuple[list[FeatureItem], list[str]]:
    """``features`` with each one ``carried`` marked passing, its test filter kept (issue
    #119), and the ids marked. ``commit_sha`` stays None: the attempt's next checkpoint stamps
    them, which also advances a plan step that covers only carried features."""
    by_id = {c.id: c for c in carried}
    out: list[FeatureItem] = []
    ids: list[str] = []
    for f in features:
        c = by_id.get(f.id)
        if c is None:
            out.append(f)
            continue
        ids.append(f.id)
        out.append(
            f.model_copy(
                update={
                    "status": FeatureStatus.PASSING,
                    "test_filter": c.test_filter or f.test_filter,
                    "commit_sha": None,
                }
            )
        )
    return out, ids
