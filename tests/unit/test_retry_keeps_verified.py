"""Issue #119 (graphcalc P26): a retry of a feature starts from the newest commit an earlier
attempt made after a passing verify, with the features it proved carried as passing, not
from the milestone head. Attempt 3 of P26 committed F1+F3 as 8b8e8b4, then halted; the
operator's retry started attempt 4 at the milestone head and spent 32 minutes rebuilding
them. Uncommitted leftovers are still discarded (I-067); with no verified commit, after a
skip or a stop, and when the milestone head moved at the stop, nothing changes."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import PerThreadResponder, ScriptedResponder
from scio.graph.nodes.prepare import carry_features
from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest
from scio.product.build import (
    MILESTONE_GATE_HEADER,
    build_thread_id,
    resume_build,
    retried_runs,
    run_build,
)
from scio.product.progress import PRODUCT_MD
from scio.state.models import (
    Budget,
    FeatureItem,
    FeatureRun,
    FeatureStatus,
    ProductBuildState,
    Tier,
)
from tests.graph.test_parallel_features import _parallel_product, _thread, _worktrees
from tests.graph.test_product_build_fake import (
    PRODUCT,
    Crash,
    _bootstrap,
    _ctx,
    _feature_state,
    _git,
)

P2_BRANCH = "agent/p2-add-and-list-todos"


class _PerAttempt(ScriptedResponder):
    """Each feature attempt behaves as ``modes`` says, keyed by the end of its thread id
    (``-p2-a1``); every other thread solves its feature.

    - ``gate``: the feature gate escalates once. It runs after ``git_checkpoint`` and the
      documenter, so the attempt has committed its verified features by then.
    - ``crash``: the process dies once at the feature gate.
    - ``idle``: the coder changes nothing, so nothing is ever verified or committed.
    - ``design``: the coder stops on a design question before any verify (T0).

    The planner's requests are kept per thread."""

    def __init__(self, modes: dict[str, str]) -> None:
        super().__init__()
        self.modes = modes
        self.planner: dict[str, list[str]] = {}
        self.fired: set[str] = set()

    def __call__(self, req: CompletionRequest) -> FakeReply:
        thread = str(req.tags.get("thread", ""))
        mode = next((m for key, m in self.modes.items() if thread.endswith(key)), "solve")
        self.coder_mode = {"idle": "idle", "design": "design_question"}.get(mode, "solve")
        if "steps" in (req.output_schema or {}).get("properties", {}):
            self.planner.setdefault(thread, []).append("\n".join(m.text() for m in req.messages))
        if _feature_gate(req) and mode in ("gate", "crash") and thread not in self.fired:
            self.fired.add(thread)
            if mode == "crash":
                raise Crash(f"killed at the gate of {thread}")
            return _escalate()
        return super().__call__(req)


def _feature_gate(req: CompletionRequest) -> bool:
    """A task graph's gate call, not the milestone gate's."""
    props = (req.output_schema or {}).get("properties", {})
    text = "\n".join(m.text() for m in req.messages)
    return "objective_met" in props and not text.startswith(MILESTONE_GATE_HEADER)


def _escalate() -> FakeReply:
    verdict = {
        "verdict": "escalate_human",
        "objective_met": False,
        "unmet_acceptance": ["(scripted) the owner wants to look first"],
        "reasoning": "scripted escalation after the checkpoint",
    }
    return FakeReply(text=json.dumps(verdict))


class _GateOnceIn:
    """Wraps a (per-thread) responder: the feature gate of the thread ending ``suffix``
    escalates once; everything else goes to ``inner``."""

    def __init__(self, inner: Any, suffix: str) -> None:
        self.inner, self.suffix, self.fired = inner, suffix, False
        self._lock = threading.Lock()

    def __call__(self, req: CompletionRequest) -> FakeReply:
        if str(req.tags.get("thread", "")).endswith(self.suffix) and _feature_gate(req):
            with self._lock:
                fire, self.fired = not self.fired, True
            if fire:
                return _escalate()
        reply: FakeReply = self.inner(req)
        return reply


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=repo)
    return r.returncode == 0


def _committed(ctx: Any, thread: str) -> tuple[str, list[str], str]:
    """The newest verified commit of a halted attempt, the features it holds, its base."""
    inner = _feature_state(ctx, thread)
    committed = [f for f in inner.feature_list if f.commit_sha]
    shas = {f.commit_sha for f in committed}
    assert len(shas) == 1, inner.task_ledger[-5:]
    assert inner.github.base_sha
    return str(shas.pop()), [f.id for f in committed], inner.github.base_sha


def _leave_leftovers(repo: Path) -> None:
    """What a halted attempt leaves behind (I-067): a stray file and a modified tracked one."""
    (repo / "stray.txt").write_text("left over\n", encoding="utf-8", newline="\n")
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "leftover\n", encoding="utf-8", newline="\n"
    )


def _no_leftovers(repo: Path) -> None:
    assert not (repo / "stray.txt").exists()
    assert "leftover" not in _git(repo, "show", "milestone/1:README.md")


# ---------- which runs a retry may start from ----------


def _run(fid: str, attempt: int, **kw: Any) -> FeatureRun:
    fields: dict[str, Any] = {"status": "failed", "milestone": "M1", **kw}
    return FeatureRun(
        feature_id=fid, thread_id=f"b-{fid.lower()}-a{attempt}", attempt=attempt, tier=Tier.T1,
        **fields,
    )  # fmt: skip


def _state(*runs: FeatureRun) -> ProductBuildState:
    return ProductBuildState(
        thread_id="b",
        product_dir=".",
        feature_runs=list(runs),
        budgets=Budget(tokens_max=1, usd_max=1.0, advisor_calls_max=0, max_iters=1, max_stall=1),
    )


def test_only_a_chain_of_retry_answers_in_this_milestone_carries() -> None:
    p1 = _run("P1", 1, status="passed")
    retry1, retry2 = _run("P2", 1, decision="retry"), _run("P2", 2, decision="retry")
    # newest first; another feature's run in between is not part of the chain, nor read
    assert retried_runs(_state(retry1, p1, retry2), "P2", "M1") == [retry2, retry1]
    # a stop, a skip, a harness failure (no one was asked) end the chain
    for ended in (
        _run("P2", 1, decision="stop"),
        _run("P2", 1, status="skipped", decision="skip"),
        _run("P2", 1, exit_reason="error"),
    ):
        assert retried_runs(_state(ended), "P2", "M1") == []
        assert retried_runs(_state(ended, retry2), "P2", "M1") == [retry2]
    # a run of an earlier milestone (a skip deferred the feature) is not this one's
    assert retried_runs(_state(_run("P2", 1, decision="retry", milestone="M1")), "P2", "M2") == []
    assert retried_runs(_state(p1), "P2", "M1") == []


def test_carried_features_start_passing_with_their_filter_and_no_commit() -> None:
    features = [
        FeatureItem(id="F1", description="one"),
        FeatureItem(id="F2", description="two", test_filter="two"),
        FeatureItem(id="F3", description="three", test_filter="planned"),
    ]
    carried = [
        FeatureItem(id="F1", description="old", test_filter="one", status=FeatureStatus.PASSING),
        FeatureItem(id="F3", description="old", status=FeatureStatus.PASSING, commit_sha="abc"),
    ]
    out, ids = carry_features(features, carried)
    assert ids == ["F1", "F3"]
    assert [(f.id, f.description, f.status, f.test_filter, f.commit_sha) for f in out] == [
        ("F1", "one", FeatureStatus.PASSING, "one", None),
        ("F2", "two", FeatureStatus.FAILING, "two", None),
        # the next checkpoint stamps a carried feature, which advances a T1 plan step that
        # covers only carried features; a commit_sha here would leave it open
        ("F3", "three", FeatureStatus.PASSING, "planned", None),
    ]


# ---------- the build (Fake provider, a real git repository) ----------


def test_a_retry_starts_from_the_verified_commit_with_its_features_passing(
    tmp_path: Path,
) -> None:
    """P26's shape on a T1 feature: attempt 1 verified and committed F1, then halted; the
    retry starts from that commit with F1 passing, discards the leftovers, rebuilds nothing,
    and the milestone branch takes the commit only once the whole feature has passed."""
    responder = _PerAttempt({"-p2-a1": "gate"})
    harness = _bootstrap(tmp_path, responder)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2", out.state.ledger[
            -3:
        ]
        a1_thread = out.interrupt["thread"]
        c1, ids, head = _committed(ctx, a1_thread)
        assert ids == ["F1"] and head == _git(repo, "rev-parse", "milestone/1")
        # a first attempt is unchanged: the milestone head, nothing carried
        a1 = _feature_state(ctx, a1_thread)
        assert a1.task.start_commit is None and a1.task.carried == []
        assert f"repository ready on branch {P2_BRANCH} at {head[:12]}" in [
            e.text for e in a1.task_ledger
        ]
        assert not _is_ancestor(repo, c1, "milestone/1")  # never a half-done feature
        _leave_leftovers(repo)

        out = resume_build(ctx, "retry")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        assert [(r.feature_id, r.attempt, r.status, r.decision) for r in s.feature_runs] == [
            ("P1", 1, "passed", None),
            ("P2", 1, "failed", "retry"),
            ("P2", 2, "passed", None),
        ]
        texts = [e.text for e in s.ledger]
        assert (
            f"P2 attempt 2 starts from {c1[:12]} (F1 verified in attempt 1), not from the "
            "head of milestone/1 (issue #119)"
        ) in texts
        a2_thread = s.feature_runs[-1].thread_id
        a2 = _feature_state(ctx, a2_thread)
    finally:
        ctx.close()
    assert a2.task.start_commit == c1 and [f.id for f in a2.task.carried] == ["F1"]
    assert "continues from commit" in a2.task.description
    # the base stays the milestone head: the diff, verify and the gate see the whole feature
    assert a2.github.base_sha == head
    inner = [e.text for e in a2.task_ledger]
    assert (
        f"repository ready on branch {P2_BRANCH} at {c1[:12]}, on top of {head[:12]}; "
        "carried as passing: F1 (issue #119)"
    ) in inner
    assert any("- F1 [passing]" in t for t in responder.planner[a2_thread])
    assert f"committed F1 as {c1[:12]}" in inner  # verified again, nothing rebuilt
    assert _is_ancestor(repo, c1, "milestone/1")  # the whole feature passed, then merged
    _no_leftovers(repo)


def test_an_attempt_that_halts_before_its_checkpoint_passes_the_verified_commit_on(
    tmp_path: Path,
) -> None:
    """T0: attempt 1 committed F1+F2 and halted at its gate; attempt 2 carried them and
    stopped on a design question before any verify, so it committed nothing. Attempt 3 still
    starts from attempt 1's commit, not from the milestone head.

    Since ADR 0020 the question parks attempt 2 and its answer gives P1 attempt 3, through a
    ``retry`` record as the owner's reply did. With GitHub the question is a comment, so no
    bookkeeping commit moves the milestone head; the answer is already in the workspace, so
    the next pass reads it and P1 goes on before P2 moves the head."""
    from scio.product import questions
    from tests.graph.test_product_build_fake import _github_product

    responder = _PerAttempt({"-p1-a1": "gate", "-p1-a2": "design"})
    ctx, _fake, repo, _origin = _github_product(tmp_path, responder)
    questions.write_answer_file(tmp_path, f"{ctx.thread_id}-p1-a2", "Keep them")  # its id
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a1")
        c1, ids, head = _committed(ctx, out.interrupt["thread"])
        assert ids == ["F1", "F2"]
        out = resume_build(ctx, "retry")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _feature_state(ctx, f"{ctx.thread_id}-p1-a2")
        assert a2.task.start_commit == c1
        assert not any(f.commit_sha for f in a2.feature_list)
    finally:
        ctx.close()
    assert [(r.feature_id, r.attempt, r.status, r.decision) for r in s.feature_runs] == [
        ("P1", 1, "failed", "retry"),
        ("P1", 2, "failed", "retry"),
        ("P1", 3, "passed", None),
        ("P2", 1, "passed", None),
    ]
    texts = [e.text for e in s.ledger]
    assert (
        f"P1 attempt 2 starts from {c1[:12]} (F1, F2 verified in attempt 1), not from the "
        "head of milestone/1 (issue #119)"
    ) in texts
    assert any(t.startswith("P1 attempt 2 parked") for t in texts)
    assert (
        f"P1 attempt 3 starts from {c1[:12]} (F1, F2 verified in attempt 1), not from the "
        "head of milestone/1 (issue #119)"
    ) in texts
    assert a2.github.base_sha == head
    assert _is_ancestor(repo, c1, "milestone/1")


def test_a_retry_after_only_uncommitted_edits_starts_from_the_milestone_head(
    tmp_path: Path,
) -> None:
    """As before #119: an attempt that never passed verify committed nothing, so the retry
    starts from the milestone head on a clean tree and carries nothing."""
    responder = _PerAttempt({"-p2-a1": "idle"})
    harness = _bootstrap(tmp_path, responder)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        a1 = _feature_state(ctx, out.interrupt["thread"])
        assert not any(f.commit_sha for f in a1.feature_list)
        head = _git(repo, "rev-parse", "milestone/1")
        _leave_leftovers(repo)
        out = resume_build(ctx, "retry")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _feature_state(ctx, s.feature_runs[-1].thread_id)
    finally:
        ctx.close()
    assert not any("starts from" in e.text for e in s.ledger)
    assert a2.task.start_commit is None and a2.task.carried == []
    assert a2.github.base_sha == head
    assert f"repository ready on branch {P2_BRANCH} at {head[:12]}" in [
        e.text for e in a2.task_ledger
    ]
    _no_leftovers(repo)


def test_a_crash_inside_the_carried_attempt_resumes_its_thread(tmp_path: Path) -> None:
    """The carried attempt's thread is checkpointed like any other: a crash at its gate
    resumes the same thread, which keeps its start and its carried features."""
    responder = _PerAttempt({"-p2-a1": "gate", "-p2-a2": "crash"})
    harness = _bootstrap(tmp_path, responder)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        c1, _, head = _committed(ctx, out.interrupt["thread"])
        with pytest.raises(Crash):
            resume_build(ctx, "retry")
        a2_thread = f"{ctx.thread_id}-p2-a2"
        assert _feature_state(ctx, a2_thread).task.start_commit == c1
        out = resume_build(ctx, None)  # crash-style resume: no answer, same thread
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _feature_state(ctx, a2_thread)
    finally:
        ctx.close()
    texts = [e.text for e in s.ledger]
    assert f"P2: resuming thread {a2_thread} (attempt 2)" in texts
    assert f"P2 attempt 2 started from {c1[:12]} (F1 carried as passing; issue #119)" in texts
    assert [(r.feature_id, r.attempt, r.status) for r in s.feature_runs] == [
        ("P1", 1, "passed"),
        ("P2", 1, "failed"),
        ("P2", 2, "passed"),
    ]
    assert a2.task.start_commit == c1 and a2.github.base_sha == head
    assert _is_ancestor(repo, c1, "milestone/1")


def test_a_milestone_head_moved_at_the_stop_starts_the_retry_from_the_head(
    tmp_path: Path,
) -> None:
    """A hand edit of PRODUCT.md made at the stop is committed on the milestone branch
    (I-109), so the verified commit no longer sits on its head. Starting from it would make
    the fast-forward onto the milestone branch fail; the retry starts from the head, and the
    ledger says why."""
    responder = _PerAttempt({"-p2-a1": "gate"})
    harness = _bootstrap(tmp_path, responder)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        c1, _, _ = _committed(ctx, out.interrupt["thread"])
        md = repo / PRODUCT_MD
        md.write_text(
            md.read_text(encoding="utf-8").replace(
                "## Vision\n", "## Vision\nEdited by hand at the stop.\n", 1
            ),
            encoding="utf-8",
            newline="\n",
        )
        out = resume_build(ctx, "retry")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _feature_state(ctx, s.feature_runs[-1].thread_id)
    finally:
        ctx.close()
    moved = a2.github.base_sha
    assert moved and "Edited by hand at the stop." in _git(repo, "show", f"{moved}:{PRODUCT_MD}")
    texts = [e.text for e in s.ledger]
    assert (
        f"P2: attempt 1's verified commit {c1[:12]} (F1) is not on top of milestone/1 at "
        f"{moved[:12]}, so this attempt starts from the head (issue #119)"
    ) in texts
    assert not any("attempt 2 starts from" in t for t in texts)
    assert a2.task.start_commit is None and a2.task.carried == []
    assert not _is_ancestor(repo, c1, "milestone/1")


def test_a_feature_the_owner_stopped_starts_from_the_head_in_the_next_build(
    tmp_path: Path,
) -> None:
    """A stop is not a retry: the next `scio build` takes the stopped run (I-165) and starts
    the feature's next attempt from the milestone head, as before."""
    responder = _PerAttempt({"-p2-a1": "gate"})
    harness = _bootstrap(tmp_path, responder)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        c1, _, _ = _committed(ctx, out.interrupt["thread"])
        out = resume_build(ctx, "stop")
        assert not out.paused and out.state.exit_reason == "feature_failed"
        assert out.state.feature_runs[-1].decision == "stop"
    finally:
        ctx.close()
    head = _git(repo, "rev-parse", "milestone/1")
    again = _ctx(tmp_path, harness, f"{build_thread_id(PRODUCT)}-r2")
    try:
        out = run_build(again)
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _feature_state(again, f"{again.thread_id}-p2-a2")
    finally:
        again.close()
    assert [(r.feature_id, r.attempt, r.status) for r in s.feature_runs] == [
        ("P1", 1, "passed"),
        ("P2", 1, "failed"),
        ("P2", 2, "passed"),
    ]
    assert not any("starts from" in e.text for e in s.ledger)
    assert a2.task.start_commit is None and a2.github.base_sha == head
    assert not _is_ancestor(repo, c1, "milestone/1")


def test_a_retry_after_a_parallel_batch_starts_from_the_head_its_sibling_moved(
    tmp_path: Path,
) -> None:
    """ADR 0018: P1 and P2 build at once in two worktrees. P1 commits F1+F2 and halts at its
    gate; P2 passes and fast-forwards the milestone branch. The retry runs P1 alone, in the
    product tree; its verified commit sits on the old head, so it starts from the new one
    (as ADR 0018 says), the ledger says why, and the batch's worktrees are gone."""
    responder = _GateOnceIn(PerThreadResponder(independent=True), "-p1-a1")
    ctx = _parallel_product(tmp_path, responder)
    repo = tmp_path / "product"
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt, out.state.ledger[-4:]
        assert out.interrupt["feature"] == "P1" and out.interrupt["thread"] == _thread("P1")
        c1, ids, base = _committed(ctx, _thread("P1"))
        assert ids == ["F1", "F2"]
        head = _git(repo, "rev-parse", "milestone/1")
        assert head != base and _is_ancestor(repo, "agent/p2-add-and-list-todos", head)
        out = resume_build(ctx, "retry")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-4:]
        a2 = _feature_state(ctx, _thread("P1", 2))
    finally:
        ctx.close()
    assert sorted((r.feature_id, r.attempt, r.status, r.decision) for r in s.feature_runs) == [
        ("P1", 1, "failed", "retry"),
        ("P1", 2, "passed", None),
        ("P2", 1, "passed", None),
    ]
    assert (
        f"P1: attempt 1's verified commit {c1[:12]} (F1, F2) is not on top of milestone/1 at "
        f"{head[:12]}, so this attempt starts from the head (issue #119)"
    ) in [e.text for e in s.ledger]
    assert a2.task.start_commit is None and a2.github.base_sha == head
    assert not _is_ancestor(repo, c1, "milestone/1")
    assert len(_worktrees(repo)) == 1  # the product tree only
