"""ADR 0020's build half (issue #141 part 5): a feature's design question parks the feature,
goes to its issue, and the answer reaches the next attempt; the milestone waits once, only
when nothing else can build.

graphcalc M4: P29's coder asked a design question at 01:38 UTC; the answer came at 03:30, and
in those 1.9 hours nothing else built, because the question stopped the whole build. Now the
attempt parks: the question is commented on the feature's issue with the ``needs-answer``
label (offline, appended to the feature's issue file), the milestone's other features build,
and an ``answer:`` comment or the workspace's answer file gives the feature its next attempt
with the answer in its task (#137's path). A failure without a question still stops for a
decision, as before.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import ScriptedResponder
from scio.graph.build import compile_graph
from scio.graph.checkpointer import open_checkpointer
from scio.graph.run_context import RunContext
from scio.harness.github.client import GitHubClient, RunResult
from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest
from scio.product import questions
from scio.product.build import build_thread_id, resume_build, run_build
from scio.state.models import (
    FeatureRun,
    MilestoneFeatureOutput,
    MilestonePlan,
    ProductBuildState,
    SCIOState,
    Tier,
)
from tests.graph.test_product_build_fake import (
    PRODUCT,
    _bootstrap,
    _ctx,
    _feature_state,
    _git,
    _harness,
    _settings,
    _subjects,
)
from tests.unit.test_github_client import FakeGh
from tests.unit.test_retry_keeps_verified import _PerAttempt

QUESTION = "Should unicode letters be kept?"
"""The Fake coder's design question (``evals/fake_scripts.py``, ``design_question``)."""
HEADING = "## The owner's answers to earlier attempts' questions"
THREAD = build_thread_id(PRODUCT)
Q1 = f"{THREAD}-p1-a1"
"""P1 attempt 1's question id: the attempt's thread id, unique across milestones and threads."""


def _offline(tmp_path: Path, modes: dict[str, str]) -> RunContext:
    """A bootstrapped Fake product without GitHub whose attempts behave as ``modes`` says
    (``design``: the coder stops on its design question; ``idle``: it changes nothing)."""
    return _ctx(tmp_path, _bootstrap(tmp_path, _PerAttempt(modes)), THREAD)


class _FlakyGh:
    """A FakeGh whose first write of each kind in ``fail_once`` fails with a 502: ``label``,
    the ``issue edit --add-label needs-answer``; ``question``, the question comment's POST.
    ``posts`` counts the question comment's POSTs, the failed one included."""

    def __init__(self, fake: FakeGh, fail_once: tuple[str, ...] = ()) -> None:
        self.fake, self.armed = fake, set(fail_once)
        self.posts = 0

    def __call__(self, argv: Any, *, stdin: str | None = None) -> RunResult:
        a = list(argv)
        kind = None
        if a[:2] == ["issue", "edit"] and questions.QUESTION_LABEL in a and "--add-label" in a:
            kind = "label"
        posting = a[0] == "api" and "POST" in a and a[3].endswith("/comments")
        if posting and "scio:question " in (stdin or ""):
            kind = "question"
            self.posts += 1
        if kind in self.armed:
            self.armed.discard(kind)
            return RunResult(1, "", "HTTP 502: Bad Gateway")
        return self.fake(a, stdin=stdin)


def _github(
    tmp_path: Path, responder: Any, *, fail_once: tuple[str, ...] = ()
) -> tuple[RunContext, FakeGh, _FlakyGh]:
    """A bootstrapped product with a bare origin and a canned ``gh`` (GitHub mode)."""
    harness = _bootstrap(tmp_path, responder)
    repo, origin = tmp_path / "product", tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    fake = FakeGh()
    fake.origin = origin
    runner = _FlakyGh(fake, fail_once)
    return _ctx(tmp_path, harness, THREAD, github=GitHubClient("o/r", runner=runner)), fake, runner


def _issue(fake: FakeGh, fid: str) -> int:
    return next(n for n, i in fake.issues.items() if str(i["title"]).startswith(f"{fid}: "))


def _labels(fake: FakeGh, n: int) -> list[str]:
    return [str(lb["name"]) for lb in fake.issues[n]["labels"]]  # type: ignore[attr-defined]


def _runs(s: ProductBuildState) -> list[tuple[str, int, str, str | None]]:
    return [(r.feature_id, r.attempt, r.status, r.decision) for r in s.feature_runs]


def _run(s: ProductBuildState, fid: str, attempt: int) -> FeatureRun:
    return next(r for r in s.feature_runs if r.feature_id == fid and r.attempt == attempt)


def _texts(s: ProductBuildState) -> list[str]:
    return [e.text for e in s.ledger]


def _thread_ended(ctx: RunContext, thread: str) -> SCIOState:
    """The attempt's own thread, which must have ended: no node left to run, no interrupt."""
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        snap = compile_graph(ctx, saver).get_state({"configurable": {"thread_id": thread}})
    assert not snap.next and not any(t.interrupts for t in snap.tasks), snap.next
    return SCIOState.model_validate(snap.values)


def _answers_section(state: SCIOState) -> str:
    description = state.task.description
    assert description.count(HEADING) == 1, description
    return description.split(HEADING, 1)[1]


# ---------- invariants 1 and 4: a question parks; the build goes on ----------


def test_a_design_question_parks_the_feature_and_the_build_goes_on(tmp_path: Path) -> None:
    """ADR 0020 decision 1. P1's coder asks a design question: P1 parks with the question and
    its id, its attempt's thread ends, and P2 builds and passes. The build's only stop is the
    milestone's questions_wait, after P2. Offline, the question is appended to P1's issue
    file and committed on the milestone branch as bookkeeping."""
    repo = tmp_path / "product"
    ctx = _offline(tmp_path, {"-p1-a1": "design"})
    try:
        out = run_build(ctx)
        s = out.state
        inner = _thread_ended(ctx, f"{THREAD}-p1-a1")
    finally:
        ctx.close()
    assert out.paused and s.phase == "questions_wait", _texts(s)[-4:]
    assert _runs(s) == [("P1", 1, "parked", None), ("P2", 1, "passed", None)]
    p1 = _run(s, "P1", 1)
    assert (p1.question, p1.question_id, p1.answer) == (QUESTION, Q1, None)  # its thread id
    assert out.interrupt is not None and "feature" not in out.interrupt  # not a feature stop
    texts = _texts(s)
    parked = next(i for i, t in enumerate(texts) if t.startswith("P1 attempt 1 parked"))
    passed = next(i for i, t in enumerate(texts) if t.startswith("P2 passed"))
    assert parked < passed and QUESTION in texts[parked]
    # the attempt's own interrupt was closed with an answer that names the park
    assert inner.phase == "halted" and any(Q1 in n for n in inner.human_notes)
    # offline: the question is in P1's issue file, committed once on the milestone branch
    q = questions.Question("P1", 1, QUESTION, qid=Q1)
    issue_file = _git(repo, "show", "milestone/1:.scio/product/issues/P1.md")
    assert questions.marker(q) in issue_file and QUESTION in issue_file
    assert sum(f"[scio:question:{Q1}]" in x for x in _subjects(repo, "milestone/1")) == 1


# ---------- invariant 2: the question goes to the feature's issue ----------


def test_the_question_is_commented_on_the_issue_with_the_needs_answer_label(
    tmp_path: Path,
) -> None:
    """With GitHub: ``format_question`` is commented on P1's issue and the issue gets the
    ``needs-answer`` label, created since the repository lacks it. The label call fails
    once, after the comment: the build goes on, the next pass adds the label, and the
    question is not posted twice, there or by the replies and the rerun at the wait."""
    ctx, fake, _runner = _github(tmp_path, _PerAttempt({"-p1-a1": "design"}), fail_once=("label",))
    try:
        out = run_build(ctx)  # the failed label halts nothing
        s = out.state
        assert out.paused and s.phase == "questions_wait", _texts(s)[-4:]
        out = resume_build(ctx, "not yet")  # read again: nothing, so it waits again
        assert out.paused and out.state.phase == "questions_wait"
        out = resume_build(ctx, None)  # the same scio build again
        assert out.paused and out.state.phase == "questions_wait"
        tail = [e.text for e in _feature_state(ctx, Q1).task_ledger[-5:]]
    finally:
        ctx.close()
    n = _issue(fake, "P1")
    assert s.issues["P1"] == n and _run(s, "P1", 1).issue_number == n
    q = questions.Question("P1", 1, QUESTION, qid=Q1)
    asked = [b for b in fake.comments.get(n, {}).values() if questions.MARKER_RE.search(b)]
    assert asked == [questions.format_question(q, tail)]
    assert asked[0].splitlines()[0] == questions.marker(q)
    assert questions.QUESTION_LABEL in _labels(fake, n)
    assert questions.QUESTION_LABEL in fake.labels
    assert _labels(fake, _issue(fake, "P2")) == ["product-mode"]  # P2 asked nothing
    assert any(
        f"adding the label {questions.QUESTION_LABEL} to issue #{n} failed" in t
        and "tried again on the next pass" in t
        for t in _texts(s)
    )
    # the stop lists the open question with its issue link
    assert out.interrupt is not None
    (open_q,) = out.interrupt["questions"]
    assert open_q["question_id"] == Q1 and open_q["link"] == f"https://github.com/o/r/issues/{n}"
    assert f"https://github.com/o/r/issues/{n}" in out.interrupt["question"]


# ---------- invariant 3: the answer goes into the next attempt ----------


class _AnswersDuringP2(_PerAttempt):
    """P1 asks; the owner answers on P1's issue while P2 builds (its first request)."""

    def __init__(self, answer: str) -> None:
        super().__init__({"-p1-a1": "design"})
        self.answer = answer
        self.fake: FakeGh | None = None
        self.answered = False
        self._once = threading.Lock()

    def __call__(self, req: CompletionRequest) -> FakeReply:
        thread = str(req.tags.get("thread", ""))
        with self._once:
            if thread.endswith("-p2-a1") and not self.answered and self.fake is not None:
                self.answered = True
                n = _issue(self.fake, "P1")
                self.fake.comments.setdefault(n, {})[self.fake.next_id] = (
                    f"Answer: {self.answer}"  # the prefix is case-insensitive
                )
                self.fake.next_id += 1
        return super().__call__(req)


def test_an_answer_comment_gives_the_parked_feature_its_next_attempt_with_the_answer(
    tmp_path: Path,
) -> None:
    """The owner answers on the issue while P2 builds. The next feature pass reads the
    answer: P1's parked run becomes a ``retry`` with the answer and its question, attempt 2's
    task carries both (#137), the label comes off and the build says it got the answer. The
    milestone never waits: it goes on to its PR."""
    answer = "Keep them, as letters"
    responder = _AnswersDuringP2(answer)
    ctx, fake, _runner = _github(tmp_path, responder)
    responder.fake = fake
    try:
        out = run_build(ctx)
        s = out.state
        a2 = _feature_state(ctx, f"{THREAD}-p1-a2")
    finally:
        ctx.close()
    assert responder.answered
    assert out.paused and s.phase == "merge_wait", _texts(s)[-4:]
    assert _runs(s) == [
        ("P1", 1, "failed", "retry"),
        ("P2", 1, "passed", None),
        ("P1", 2, "passed", None),
    ]
    a1 = _run(s, "P1", 1)
    assert (a1.question, a1.answer, a1.question_id) == (QUESTION, answer, Q1)
    section = _answers_section(a2)
    assert f"Question: {QUESTION}" in section and f"Answer: {answer}" in section
    assert "Attempt 1" in section
    n = _issue(fake, "P1")
    assert questions.QUESTION_LABEL not in _labels(fake, n)
    bodies = list(fake.comments[n].values())
    received = [b for b in bodies if "answer-received" in b and Q1 in b]
    assert len(received) == 1 and not received[0].lower().startswith(questions.ANSWER_PREFIX)
    assert not questions.MARKER_RE.search(received[0])
    assert any(t.startswith(f"P1: the answer to {Q1} was received") for t in _texts(s))
    assert not any("questions_wait" in t for t in _texts(s))


def test_an_answer_file_gives_the_parked_feature_its_next_attempt(tmp_path: Path) -> None:
    """Offline, the answer is the workspace's answer file, written as the dashboard writes it
    (``write_answer_file`` under its ``--root``): the directory that holds the run's ``.scio``
    work directory, with its ledger. A rerun of ``scio build`` with nothing answered waits
    again; a rerun after the file is written gives P1 attempt 2 with the answer."""
    answer = "Keep them, as letters"
    ctx = _offline(tmp_path, {"-p1-a1": "design"})
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "questions_wait"
        assert out.interrupt is not None
        (open_q,) = out.interrupt["questions"]
        out = resume_build(ctx, None)  # a rerun, nothing answered yet: it waits again
        assert out.paused and out.state.phase == "questions_wait"
        # the dashboard's root is the workspace: the directory holding the run's .scio
        assert (tmp_path / ".scio" / "ledger" / f"{THREAD}.jsonl").is_file()
        path = questions.write_answer_file(tmp_path, Q1, answer)
        assert open_q["answer_file"] == str(path)
        out = resume_build(ctx, None)  # the next rerun reads it
        s = out.state
        a2 = _feature_state(ctx, f"{THREAD}-p1-a2")
    finally:
        ctx.close()
    assert out.paused and s.phase == "merge_wait", _texts(s)[-4:]
    assert _runs(s) == [
        ("P1", 1, "failed", "retry"),
        ("P2", 1, "passed", None),
        ("P1", 2, "passed", None),
    ]
    assert _run(s, "P1", 1).answer == answer
    section = _answers_section(a2)
    assert f"Question: {QUESTION}" in section and f"Answer: {answer}" in section


def test_the_build_reads_answers_from_the_directory_it_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``scio build`` runs from the workspace with the default ``.scio`` work directory and
    checkpoint DB, the directory the dashboard is given as ``--root``: the build's answer
    directory is that one's, wherever the product lives."""
    from scio.product.build import answers_workspace

    workspace = tmp_path / "graphcalc-run"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    harness = _bootstrap(tmp_path)
    ctx = RunContext.create(
        thread_id=THREAD, sandbox_root=tmp_path / "product", settings=harness.settings,
        harness=harness,
    )  # fmt: skip
    try:
        assert answers_workspace(ctx) == workspace.resolve()
        path = questions.write_answer_file(workspace, Q1, "yes")
        assert questions.read_answer_file(answers_workspace(ctx), Q1) == "yes"
        assert path == workspace / ".scio" / "answers" / f"{Q1}.md"
    finally:
        ctx.close()


# ---------- invariant 5: the milestone waits once ----------


def test_the_milestone_waits_once_when_only_parked_features_remain(tmp_path: Path) -> None:
    """P1 and P2 both ask. Neither stops the build on its own: once nothing else can build,
    the milestone stops once, at questions_wait, listing both questions. ``stop`` there ends
    the build with both still parked."""
    ctx = _offline(tmp_path, {"-p1-a1": "design", "-p2-a1": "design"})
    try:
        out = run_build(ctx)
        s = out.state
        assert out.paused and s.phase == "questions_wait", _texts(s)[-4:]
        assert out.interrupt is not None
        stop: dict[str, Any] = dict(out.interrupt)
        out = resume_build(ctx, "stop")
    finally:
        ctx.close()
    assert _runs(s) == [("P1", 1, "parked", None), ("P2", 1, "parked", None)]
    assert [q["question_id"] for q in stop["questions"]] == [Q1, f"{THREAD}-p2-a1"]
    assert all(q["question"] for q in stop["questions"])
    assert "proceed" in stop["question"] and "stop" in stop["question"]
    assert stop["question"].count(".scio/product/issues/") == 2
    assert sum(t.startswith("M1 waits at questions_wait") for t in _texts(s)) == 1
    assert not out.paused and out.state.exit_reason == "stopped"
    assert _runs(out.state) == [("P1", 1, "parked", None), ("P2", 1, "parked", None)]


# ---------- invariant 6: proceed defers, as a skip does ----------


def test_proceed_at_questions_wait_defers_the_parked_features(tmp_path: Path) -> None:
    """``proceed`` records each parked feature as a skip records it (deferred, still open),
    so #140's gate does not judge it and the milestone goes on to its PR. The issue no
    longer waits: its ``needs-answer`` label comes off, once, and the build comments that the
    owner deferred the feature to the next milestone."""
    ctx, fake, _runner = _github(tmp_path, _PerAttempt({"-p1-a1": "design"}))
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "questions_wait"
        n = _issue(fake, "P1")
        assert questions.QUESTION_LABEL in _labels(fake, n)
        out = resume_build(ctx, "proceed")
        s = out.state
    finally:
        ctx.close()
    assert out.paused and s.phase == "merge_wait", _texts(s)[-4:]
    assert _runs(s) == [("P1", 1, "skipped", "skip"), ("P2", 1, "passed", None)]
    assert _run(s, "P1", 1).question_id == Q1  # still says it was a question
    gate = s.last_gate
    assert gate is not None and gate.verdict == "pass", gate
    assert not gate.reasoning.startswith("overridden"), gate.reasoning
    # the issue: the label is off, and one comment says the owner deferred P1
    assert questions.QUESTION_LABEL not in _labels(fake, n)
    deferred = [b for b in fake.comments[n].values() if f"scio:deferred id={Q1}" in b]
    assert len(deferred) == 1 and "deferred P1 to the next milestone" in deferred[0]
    assert not questions.MARKER_RE.search(deferred[0])
    assert not deferred[0].lower().startswith(questions.ANSWER_PREFIX)
    removes = [a for a, _ in fake.calls if a[:2] == ["issue", "edit"] and "--remove-label" in a]
    assert len(removes) == 1
    # the PR body says P1 is deferred with its question unanswered, and leaves its issue open
    assert s.pr is not None and s.pr.number is not None
    body = str(next(p for p in fake.prs.values() if p["number"] == s.pr.number)["body"])
    assert f"skipped (question {Q1} unanswered)" in body
    assert f"Refs #{n} (P1 deferred, not closed)" in body


# ---------- invariant 7: a failure without a question still stops ----------


def test_a_failure_without_a_question_still_stops_for_a_decision(tmp_path: Path) -> None:
    """P1 parks on its question; P2 fails without one (an idle coder): P2 still stops the
    build for today's decision, ``retry``, ``skip`` or stop. ``skip`` defers P2, and the
    milestone then waits for P1's answer."""
    ctx = _offline(tmp_path, {"-p1-a1": "design", "-p2-a1": "idle"})
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt is not None, _texts(out.state)[-4:]
        stop: dict[str, Any] = dict(out.interrupt)
        out = resume_build(ctx, "skip")
        s = out.state
    finally:
        ctx.close()
    assert stop["feature"] == "P2" and stop["thread"].endswith("-p2-a1")
    assert (
        "'skip' to defer it" in stop["question"]
        and "<retry[: your answer]|skip|stop>" in (stop["resume"])
    )
    assert out.paused and s.phase == "questions_wait", _texts(s)[-4:]
    assert _runs(s) == [("P1", 1, "parked", None), ("P2", 1, "skipped", "skip")]
    assert _run(s, "P2", 1).question_id is None


# ---------- a batch (ADR 0018) passes a parked feature over too ----------


class _BatchModes:
    """One ScriptedResponder per OS thread and mode: a batch calls the provider from several
    feature threads at once (``PerThreadResponder``), and P1's attempt 1 asks its question."""

    def __init__(self, modes: dict[str, str]) -> None:
        self.modes = modes
        self.inner: dict[tuple[int, str], ScriptedResponder] = {}
        self._lock = threading.Lock()

    def __call__(self, req: CompletionRequest) -> FakeReply:
        thread = str(req.tags.get("thread", ""))
        mode = next((m for k, m in self.modes.items() if thread.endswith(k)), "solve")
        with self._lock:
            key = (threading.get_ident(), mode)
            if key not in self.inner:
                self.inner[key] = ScriptedResponder(independent=True, coder_mode=mode)
            r = self.inner[key]
        return r(req)


def test_a_batch_parks_its_questioning_feature_and_merges_the_other(tmp_path: Path) -> None:
    """P1 and P2 build at once (``parallel_features=2``): P1 parks, P2 passes and is merged,
    and nothing stops until the milestone has only P1's question left. P1's worktree goes
    once its run is recorded. The answer file then gives P1 its attempt 2, alone."""
    from tests.graph.test_parallel_features import _bootstrap as _batch_bootstrap
    from tests.graph.test_parallel_features import _build_ctx

    harness = _batch_bootstrap(tmp_path, _BatchModes({"-p1-a1": "design_question"}))
    ctx = _build_ctx(tmp_path, harness, 2)
    try:
        out = run_build(ctx)
        s = out.state
        assert out.paused and s.phase == "questions_wait", _texts(s)[-4:]
        questions.write_answer_file(tmp_path, Q1, "Keep them")
        done = resume_build(ctx, "answered")
        a2 = _feature_state(ctx, f"{THREAD}-p1-a2")
    finally:
        ctx.close()
    assert _runs(s) == [("P1", 1, "parked", None), ("P2", 1, "passed", None)]
    texts = _texts(s)
    assert any(t.startswith("2 features at once") for t in texts)
    assert any(t.startswith("P1 attempt 1 parked") for t in texts)
    assert not (tmp_path / ".scio" / "worktrees" / f"{THREAD}-p1-a1").exists()
    assert done.paused and done.state.phase == "merge_wait", _texts(done.state)[-4:]
    assert _runs(done.state)[-1] == ("P1", 2, "passed", None)
    assert "Answer: Keep them" in _answers_section(a2)


# ---------- a stop asked before ADR 0020 keeps the owner's reply ----------


def test_a_stop_asked_before_the_upgrade_still_takes_the_owners_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that stopped at P1's feature decision on a design question before this
    change (the old code asked every question there) is answered ``retry: <answer>`` after
    it. The node run that the reply starts finds the question, but the owner was already
    asked about this attempt: the reply answers that stop, as it did (#137), and is not
    dropped by a park."""
    import scio.product.build as build

    ctx = _offline(tmp_path, {"-p1-a1": "design"})
    try:
        with monkeypatch.context() as m:
            m.setattr(build, "asked_question", lambda _final: None)  # the old code: no park
            out = run_build(ctx)
        assert out.paused and out.interrupt is not None
        assert out.interrupt["feature"] == "P1" and out.interrupt["thread"].endswith("-p1-a1")
        out = resume_build(ctx, "retry: Keep them")
        s = out.state
        a2 = _feature_state(ctx, f"{THREAD}-p1-a2")
    finally:
        ctx.close()
    assert out.paused and s.phase == "merge_wait", _texts(s)[-4:]
    a1 = _run(s, "P1", 1)
    assert (a1.status, a1.decision, a1.answer) == ("failed", "retry", "Keep them")
    assert a1.question == QUESTION and a1.question_id is None
    assert "Answer: Keep them" in _answers_section(a2)
    assert not any("parked" in t for t in _texts(s))


# ---------- a question's id is unique; a failed post never halts the build ----------


def test_a_question_id_is_unique_across_milestones(tmp_path: Path) -> None:
    """Attempts are numbered from 1 again in a new build thread and in a later milestone:
    P29's first attempt in M4, in M4's rerun thread ``-r2`` and in M5 would all be ``P29-a1``.
    The id is the attempt's thread id, so each is its own question. The old one's marker does
    not stop the new one from being posted, and the old answer, on the issue or in the answer
    file, is never read for the new one."""
    import scio.product.build as build

    fake = FakeGh()
    fake.issues[7] = {"number": 7, "title": "P29: dots", "body": "", "labels": [], "url": "u"}
    (tmp_path / "product").mkdir()
    harness = _harness(_settings(tmp_path), ScriptedResponder())
    client = GitHubClient("o/r", runner=fake)
    ctx = _ctx(tmp_path, harness, "build-graphcalc-m5", github=client)
    plan = MilestonePlan(
        number=5, title="m5", branch="milestone/5", features=[MilestoneFeatureOutput(id="P29")]
    )

    def parked(thread: str, milestone: str) -> FeatureRun:
        """What parking records for P29's attempt 1 on ``thread``."""
        return FeatureRun(
            feature_id="P29", thread_id=thread, attempt=1, tier=Tier.T0, status="parked",
            issue_number=7, milestone=milestone, question="Which rule?", question_id=thread,
        )  # fmt: skip

    old = parked("build-graphcalc-m4-p29-a1", "M4")
    later = [
        parked("build-graphcalc-m4-r2-p29-a1", "M4"),
        parked("build-graphcalc-m5-p29-a1", "M5"),
    ]
    try:
        build._ask(ctx, plan, old, [])
        fake.comments[7][fake.next_id] = "answer: rule A"  # the owner answers the M4 question
        fake.next_id += 1
        questions.write_answer_file(tmp_path, old.thread_id, "rule A")
        assert build.read_answer(ctx, old)[0] == "rule A"
        for run in later:
            build._ask(ctx, plan, run, [])
            assert build.read_answer(ctx, run)[0] is None, run.thread_id
    finally:
        ctx.close()
    asked = [m.group(3) for b in fake.comments[7].values() if (m := questions.MARKER_RE.search(b))]
    assert asked == [old.thread_id, *(r.thread_id for r in later)]
    assert questions.question_id("P29", 1) == "P29-a1"  # what all three would have shared


def test_a_failed_question_post_keeps_the_feature_parked_and_retries_next_pass(
    tmp_path: Path,
) -> None:
    """The question's comment fails once (a 502). The build does not halt: P1 stays parked,
    the failure is a ledger line, and the next pass posts the question, once, and only then
    adds the label; P2 builds meanwhile."""
    responder = _PerAttempt({"-p1-a1": "design"})
    ctx, fake, runner = _github(tmp_path, responder, fail_once=("question",))
    try:
        out = run_build(ctx)
        s = out.state
    finally:
        ctx.close()
    assert out.paused and s.phase == "questions_wait" and s.exit_reason is None, _texts(s)[-4:]
    assert _runs(s) == [("P1", 1, "parked", None), ("P2", 1, "passed", None)]
    n = _issue(fake, "P1")
    texts = _texts(s)
    failed = [t for t in texts if f"posting question {Q1} on issue #{n} failed" in t]
    assert len(failed) == 1 and "tried again on the next pass" in failed[0]
    assert runner.posts == 2  # the failed POST, then the next pass's
    asked = [b for b in fake.comments[n].values() if questions.MARKER_RE.search(b)]
    assert len(asked) == 1 and questions.QUESTION_LABEL in _labels(fake, n)
    calls = [a for a, _ in fake.calls]
    comments = f"repos/o/r/issues/{n}/comments"
    posted = next(i for i, a in enumerate(calls) if a[:4] == ["api", "-X", "POST", comments])
    labelled = next(i for i, a in enumerate(calls) if a[:2] == ["issue", "edit"])
    assert posted < labelled  # no label on an issue whose question is not there yet
