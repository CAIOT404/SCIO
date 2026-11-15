"""Issue #137 (graphcalc M4 P29): the owner's answer to a coder's design question reaches
the retried attempt, in its task, next to the question it answers.

P29's coder stopped on ``design decision needed: ...`` (two ways to close "1.2.3", which
one?). The feature stop offered ``retry``, ``skip`` or stop, and a ``retry`` rebuilt the
task from the spec loaded when the milestone started: the owner's one-sentence answer had
no way to reach the next attempt. Now ``retry: <answer>`` records the answer and the
question on the run, and every later attempt of the feature in the milestone reads them in
its task's description. A plain ``retry`` builds the same task as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scio.graph.run_context import RunContext
from scio.product.build import build_thread_id, feature_task, resume_build, run_build
from scio.state.models import FeatureRun, ProductBuildState, SCIOState
from tests.graph.test_product_build_fake import PRODUCT, _bootstrap, _ctx, _feature_state
from tests.unit.test_retry_keeps_verified import _PerAttempt

QUESTION = "Should unicode letters be kept?"
"""The Fake coder's design question (``evals/fake_scripts.py``, ``design_question``)."""
HEADING = "## The owner's answers to earlier attempts' questions"


def _stop_line(attempt: int) -> str:
    """The question a P1 stop carries when the attempt asked none (``stop_question``): since
    ADR 0020 a design question parks the feature instead of stopping the build, so these
    stops come from the feature gate's own escalation (``gate``)."""
    return f"Feature P1 (escalate_human, attempt {attempt})"


def _build(tmp_path: Path, modes: dict[str, str]) -> RunContext:
    """A bootstrapped Fake product whose P1 attempts behave as ``modes`` says (``gate``: the
    feature gate escalates once, a stop without a question; ``idle``: the coder changes
    nothing); every other attempt solves its feature."""
    harness = _bootstrap(tmp_path, _PerAttempt(modes))
    return _ctx(tmp_path, harness, build_thread_id(PRODUCT))


def _attempt(ctx: RunContext, n: int) -> SCIOState:
    return _feature_state(ctx, f"{ctx.thread_id}-p1-a{n}")


def _run(state: ProductBuildState, n: int) -> FeatureRun:
    return next(r for r in state.feature_runs if r.thread_id.endswith(f"-p1-a{n}"))


def _plain_description(state: ProductBuildState) -> str:
    """What ``feature_task`` builds for P1 without answers: the task before #137."""
    assert state.spec is not None and state.milestone is not None
    feat = next(f for f in state.milestone.features if f.id == "P1")
    task = feature_task(
        state.spec, state.milestone, feat, state.issues.get("P1"), repo_dir=state.product_dir
    )
    return task.description


def _section(description: str) -> str:
    assert description.count(HEADING) == 1, description
    return description.split(HEADING, 1)[1]


def test_a_retry_with_an_answer_carries_it_and_the_question_into_the_next_task(
    tmp_path: Path,
) -> None:
    """A feature stop (``escalate_human``, the attempt's own interrupt); the owner answers
    ``retry: <answer>``; attempt 2's task holds the question and the answer, in the owner's
    case. The stop is a pending interrupt, so a build halted there before the upgrade
    resumes the same way. (P29's design question now parks the feature, ADR 0020:
    ``tests/unit/test_question_channel.py``.)"""
    ctx = _build(tmp_path, {"-p1-a1": "gate"})
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a1")
        stop: dict[str, Any] = dict(out.interrupt)
        out = resume_build(ctx, "retry: Use rule A, and keep '1.2.3' an error")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _attempt(ctx, 2)
    finally:
        ctx.close()
    answer = "Use rule A, and keep '1.2.3' an error"
    description = a2.task.description
    section = _section(description)
    assert f"Question: {_stop_line(1)}" in section and f"Answer: {answer}" in section
    assert section.index(_stop_line(1)) < section.index(answer)
    assert "Attempt 1" in section
    # the rest of the task is the one a plain retry builds, then the answers (and the note
    # that attempt 2 continues from attempt 1's verified commit, #119)
    assert description.startswith(_plain_description(s) + "\n\n" + HEADING)
    a1 = _run(s, 1)
    assert a1.decision == "retry" and a1.status == "failed"
    assert a1.answer == answer and a1.question == _stop_line(1)  # it asked no question
    # the stop said how: an answer after `retry`, carried with the question
    assert "'retry: <your answer>'" in stop["question"] and "with the question" in stop["question"]
    assert "'skip' to defer it" in stop["question"]  # the rest of the stop is unchanged
    assert "<retry[: your answer]|skip|stop>" in stop["resume"]


def test_a_plain_retry_builds_the_same_task_as_before(tmp_path: Path) -> None:
    """A plain ``retry`` (any case) records no question or answer, and the next attempt's
    task is ``feature_task`` without answers, byte for byte. (An idle attempt commits
    nothing, so no verified commit is carried either, #119.)"""
    ctx = _build(tmp_path, {"-p1-a1": "idle"})
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a1")
        out = resume_build(ctx, "  Retry  ")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2 = _attempt(ctx, 2)
    finally:
        ctx.close()
    assert a2.task.description == _plain_description(s)
    assert HEADING not in a2.task.description
    a1 = _run(s, 1)
    assert a1.decision == "retry" and (a1.question, a1.answer) == (None, None)
    assert [(r.feature_id, r.attempt, r.status) for r in s.feature_runs] == [
        ("P1", 1, "failed"),
        ("P1", 2, "passed"),
        ("P2", 1, "passed"),
    ]


def test_every_answered_retry_of_a_feature_reaches_its_later_attempts(tmp_path: Path) -> None:
    """Answers accumulate, oldest first, each under its attempt's number: attempt 1 is
    answered, attempt 2 is retried plainly, attempt 3 is answered; attempts 2 and 3 still
    read attempt 1's answer, and attempt 4 reads both."""
    ctx = _build(tmp_path, {"-p1-a1": "gate", "-p1-a2": "gate", "-p1-a3": "gate"})
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a1")
        out = resume_build(ctx, "retry: use rule A")
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a2")
        out = resume_build(ctx, "retry")
        assert out.paused and out.interrupt and out.interrupt["thread"].endswith("-p1-a3")
        out = resume_build(ctx, "RETRY - and rule B for a trailing dot")
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-3:]
        a2, a3, a4 = (_attempt(ctx, n) for n in (2, 3, 4))
    finally:
        ctx.close()
    first, second = "use rule A", "and rule B for a trailing dot"
    for later in (a2, a3):
        section = _section(later.task.description)
        assert first in section and second not in section and "Attempt 1" in section
    section = _section(a4.task.description)
    assert section.index("Attempt 1") < section.index(first) < section.index("Attempt 3")
    assert section.index("Attempt 3") < section.index(second)
    assert "Attempt 2" not in section  # a plain retry answered nothing
    assert _stop_line(1) in section and _stop_line(3) in section
    assert _stop_line(2) not in section
    assert [(r.attempt, r.decision, r.answer) for r in s.feature_runs if r.feature_id == "P1"] == [
        (1, "retry", first),
        (2, "retry", None),
        (3, "retry", second),
        (4, None, None),
    ]


def test_a_feature_run_recorded_before_the_answer_fields_still_loads() -> None:
    """A checkpoint written before #137 holds runs without ``question`` and ``answer``:
    they load with both None, and an old ``retry`` run carries no answer into a task."""
    old: dict[str, Any] = {
        "feature_id": "P29",
        "thread_id": "build-graphcalc-m4-p29-a1",
        "attempt": 1,
        "tier": "T0",
        "status": "failed",
        "exit_reason": "escalate_human",
        "milestone": "M4",
        "decision": "retry",
        "human_notes": ["human decision: retry"],
    }
    run = FeatureRun.model_validate(old)
    assert (run.question, run.answer) == (None, None)
    state = ProductBuildState.model_validate(
        {
            "thread_id": "build-graphcalc-m4",
            "product_dir": ".",
            "feature_runs": [old],
            "budgets": {
                "tokens_max": 1,
                "usd_max": 1.0,
                "advisor_calls_max": 0,
                "max_iters": 1,
                "max_stall": 1,
            },
        }
    )
    assert state.feature_runs == [run]
    from scio.product.build import owner_answers

    assert owner_answers(state, "P29", "M4") == []
    # the new fields round-trip through a checkpoint
    answered = run.model_copy(update={"question": "q?", "answer": "a."})
    assert FeatureRun.model_validate(answered.model_dump(mode="json")) == answered


def test_the_answer_is_the_text_after_retry_in_the_owners_case() -> None:
    from scio.product.build import retry_answer

    assert retry_answer("retry") is None
    assert retry_answer("  RETRY  ") is None
    assert retry_answer("retry:") is None
    assert retry_answer("retry: Use rule A") == "Use rule A"
    assert retry_answer("Retry - keep '5.' Valid") == "keep '5.' Valid"
    assert retry_answer("retry,  B: then C") == "B: then C"
    assert retry_answer("retry  :- , x") == "x"
    # the keyword must end where a separator or the reply does: a plain retry, as before
    assert retry_answer("retrying please") is None
    assert retry_answer("Retrying: use rule A") is None
    assert retry_answer("retry.") is None


def test_a_t1_design_question_is_carried_from_the_worker_output() -> None:
    """On T1/T2 the coder's design question goes to the advisor first and no ledger line
    names it; when the stop reaches the owner, the question is the worker's own
    ``design_question``. It also comes before the T0 ledger line, and the stop's own line
    is the last resort."""
    from scio.product.build import stop_question
    from scio.state.models import Budget, GitHubRefs, Role, TaskSpec, WorkerOutput

    def state(design_question: str | None, ledger: list[str]) -> SCIOState:
        worker = WorkerOutput(
            step_id="S1",
            summary="blocked on a design decision",
            files_touched=[],
            confidence="low",
            needs_design_decision=design_question is not None,
            design_question=design_question,
            next_action="verify",
        )
        return SCIOState.model_validate(
            {
                "thread_id": "build-graphcalc-m4-p29-a1",
                "task": TaskSpec(title="P29", description="d", acceptance=["a"], repo_url="."),
                "budgets": Budget(tokens_max=1, usd_max=1.0, advisor_calls_max=1),
                "github": GitHubRefs(owner="o", repo="r"),
                "last_worker_output": worker,
                "task_ledger": [
                    {"kind": "decision", "source": Role.CODER, "text": t} for t in ledger
                ],
            }
        )

    asked = "Which rule closes '1.2.3': (A) or (B)?"
    t1 = state(asked, ["advisor: escalate_human (the owner must choose)"])
    assert stop_question(t1, "P29", "escalate_human", 1) == asked
    # the worker's question outranks a ledger line; whitespace alone is no question
    both = state(asked, [f"design decision needed: {QUESTION}"])
    assert stop_question(both, "P29", "escalate_human", 1) == asked
    blank = state("  ", [f"design decision needed: {QUESTION}"])
    assert stop_question(blank, "P29", "escalate_human", 1) == f"design decision needed: {QUESTION}"
    none = state(None, ["verify: no_changes (iteration 3, stall 2)"])
    assert stop_question(none, "P29", "stall", 2) == "Feature P29 (stall, attempt 2)"


def test_the_answers_section_lists_each_answer_under_its_attempt() -> None:
    """``feature_task``'s answers: one heading, oldest first, each attempt's number, its
    question and its answer; none adds nothing at all."""
    from scio.state.models import MilestoneFeatureOutput, MilestonePlan, ProductSpec

    spec = ProductSpec.model_validate(
        {
            "name": "calc",
            "title": "Calc",
            "vision": "A calculator.",
            "users": ["a student"],
            "quality_bar": ["tests pass"],
            "features": [
                {"id": "P1", "description": "Numbers", "acceptance": ["`.5` is accepted"]},
            ],
        }
    )
    plan = MilestonePlan.model_validate(
        {"number": 1, "title": "One", "branch": "milestone/1", "features": [
            {"id": "P1", "tier": "T0"},
        ]}
    )  # fmt: skip
    feat: MilestoneFeatureOutput = plan.features[0]
    plain = feature_task(spec, plan, feat, 3).description
    assert feature_task(spec, plan, feat, 3, answers=()).description == plain
    task = feature_task(spec, plan, feat, 3, answers=[(1, "Q one?", "A one."), (3, "Q3", "A3")])
    assert task.description == "\n".join(
        [
            plain,
            "",
            HEADING,
            "Attempt 1",
            "Question: Q one?",
            "Answer: A one.",
            "",
            "Attempt 3",
            "Question: Q3",
            "Answer: A3",
        ]
    )
