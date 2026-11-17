"""Issue #172: an advisor ``continue`` settles the drift it ruled on, and a step whose one
consult is used goes back to work while the run's total has headroom.

The graphcalc run's P15 attempt 1 (``build-graphcalc-m4-p15-a1``): step S4 drifted onto
``graphcalc/numfmt.py``, the advisor answered ``continue`` ("The design is right ... Keep
it"), the next session fixed the regression, and the verify after it halted on
``advisor already consulted on step S4 (one call per step; 2/3 for this run); plan_drift on
graphcalc/numfmt.py``: 51 minutes and $6.49 for an attempt the advisor had approved.
"""

from __future__ import annotations

import pytest

from scio.loop.escalation import EscalationConfig, approve_drift, decide, detect, exit_for
from scio.state.models import (
    Budget,
    EscalationTrigger,
    ExitReason,
    GitHubRefs,
    Plan,
    PlanStep,
    ProgressLedger,
    Role,
    SCIOState,
    TaskSpec,
    Verdict,
    WorkerOutput,
)

CFG = EscalationConfig(repeat_threshold=3, max_stall=2, high_impact_files=8)
ROOT = "/w"
PLANNED = "graphcalc/lists.py"
DRIFTED = "graphcalc/numfmt.py"
DRIFT = [EscalationTrigger.PLAN_DRIFT]


def _budget(*, used: int = 1, cap: int = 3, on_step: dict[str, int] | None = None) -> Budget:
    return Budget(
        tokens_max=10**6,
        usd_max=10.0,
        advisor_calls_max=cap,
        advisor_calls_used=used,
        advisor_calls_on_step=on_step or {},
    )


def _state(**overrides: object) -> SCIOState:
    base: dict[str, object] = dict(
        thread_id="build-graphcalc-m4-p15-a1",
        task=TaskSpec(title="t", description="d", acceptance=["a"], repo_url="r"),
        budgets=_budget(),
        github=GitHubRefs(owner="o", repo="r"),
        plan=Plan(
            author=Role.PLANNER,
            steps=[
                PlanStep(id="S4", description="d", feature_ids=["F1"], files_expected=[PLANNED])
            ],
            rationale="r",
        ),
        progress=ProgressLedger(current_step_id="S4", last_failure_kind="tests"),
        last_worker_output=_worker(),
    )
    base.update(overrides)
    return SCIOState(**base)  # type: ignore[arg-type]


def _worker(*files: str, confidence: str = "high") -> WorkerOutput:
    touched = list(files) or [PLANNED, DRIFTED]
    return WorkerOutput(
        step_id="S4",
        summary="s",
        files_touched=[f"{ROOT}/{f}" for f in touched],
        confidence=confidence,  # type: ignore[arg-type]
        next_action="verify",
    )


def _ruled(state: SCIOState, verdict: Verdict = Verdict.CONTINUE) -> SCIOState:
    """The state after the advisor's verdict on ``state``'s consult: the step's one call is
    used, and the approval is whatever ``approve_drift`` records for that verdict."""
    assert state.last_worker_output is not None
    approved = approve_drift(
        state,
        CFG,
        verdict=verdict,
        triggers=detect(state, CFG, root=ROOT),
        touched=state.last_worker_output.files_touched,
        root=ROOT,
    )
    used = state.budgets.advisor_calls_used + 1
    budgets = state.budgets.model_copy(
        update={"advisor_calls_used": used, "advisor_calls_on_step": {"S4": 1}}
    )
    return state.model_copy(update={"drift_approved": approved, "budgets": budgets})


def test_a_drift_the_advisor_continued_does_not_fire_again_on_that_step() -> None:
    """Invariant 1, the owner's words: "an advisor continue clears the drift it ruled on"."""
    before = _state()
    first = decide(before, CFG, root=ROOT)
    assert first.route == "advise" and first.triggers == DRIFT
    assert first.reason == f"plan_drift; plan_drift on {DRIFTED}"

    after = _ruled(before)
    assert after.drift_approved == {"v1:S4": [DRIFTED]}
    assert detect(after, CFG, root=ROOT) == []
    d = decide(after, CFG, root=ROOT)  # the same files, the same step: an ordinary failure
    assert d.route == "debug" and d.triggers == []
    assert d.reason == "no escalation signal; clear failure path"


def test_a_new_drifted_file_after_the_ruling_still_fires() -> None:
    """Invariant 2: a file outside the plan and outside the ruling is a new drift, and the
    reason names only that file. With run headroom the step keeps working (invariant 4)."""
    new = "graphcalc/evaluate.py"
    after = _ruled(_state()).model_copy(
        update={"last_worker_output": _worker(PLANNED, DRIFTED, new)}
    )
    assert detect(after, CFG, root=ROOT) == DRIFT
    d = decide(after, CFG, root=ROOT)
    assert d.triggers == DRIFT
    assert d.reason.endswith(f"; plan_drift on {new}")
    assert DRIFTED not in d.reason

    # the ruling was for S4: the same file on another step is that step's own drift
    plan = Plan(
        author=Role.PLANNER,
        steps=[PlanStep(id="S5", description="d", feature_ids=["F1"], files_expected=[PLANNED])],
        rationale="r",
    )
    other = after.model_copy(
        update={
            "plan": plan,
            "progress": ProgressLedger(current_step_id="S5", last_failure_kind="tests"),
            "last_worker_output": _worker(),
        }
    )
    d5 = decide(other, CFG, root=ROOT)
    assert d5.route == "advise"
    assert d5.reason == f"plan_drift; plan_drift on {DRIFTED}"


def test_a_replan_reusing_the_step_id_starts_with_no_approval() -> None:
    """Identity: a replan's steps reuse ids. The approval is keyed by plan version and step
    id, so plan v2's S4 has none of v1's."""
    after = _ruled(_state())
    assert after.plan is not None
    v2 = after.plan.model_copy(update={"version": 2})
    replanned = after.model_copy(update={"plan": v2})
    assert detect(replanned, CFG, root=ROOT) == DRIFT


@pytest.mark.parametrize(
    "verdict", [Verdict.REPLAN, Verdict.CHANGE_APPROACH, Verdict.ESCALATE_HUMAN]
)
def test_a_replan_or_change_approach_verdict_approves_no_drift(verdict: Verdict) -> None:
    """Invariant 3: only ``continue`` approves."""
    after = _ruled(_state(), verdict)
    assert after.drift_approved == {}
    assert detect(after, CFG, root=ROOT) == DRIFT


def test_a_continue_on_a_consult_without_drift_approves_nothing() -> None:
    """A ``continue`` rules on what the consult was about. A low-confidence consult said
    nothing about the files, so a file that drifts is not approved by it."""
    state = _state()
    approved = approve_drift(
        state,
        CFG,
        verdict=Verdict.CONTINUE,
        triggers=[EscalationTrigger.LOW_CONFIDENCE],
        touched=[f"{ROOT}/{DRIFTED}"],
        root=ROOT,
    )
    assert approved == {}


def test_a_used_step_consult_with_run_headroom_keeps_the_step_working() -> None:
    """Invariant 4, P15's sequence: drift, consult ``continue``, then low confidence with the
    same drift, the step's consult used and 2 of 3 for the run. It routes to ``debug``."""
    consulted = _state(budgets=_budget(used=1, on_step={"S3": 1}))
    assert decide(consulted, CFG, root=ROOT).route == "advise"
    after = _ruled(consulted).model_copy(
        update={"last_worker_output": _worker(PLANNED, DRIFTED, confidence="low")}
    )
    assert after.budgets.advisor_calls_used == 2 and not after.budgets.advisor_allowed("S4")

    d = decide(after, CFG, root=ROOT)
    assert d.route == "debug"
    assert d.triggers == [EscalationTrigger.LOW_CONFIDENCE]  # kept, for the ledger
    assert d.reason == (
        "advisor already consulted on step S4 (one call per step; 2/3 for this run); "
        "the step continues"
    )
    assert exit_for(d) is None  # no advisor_cap exit: the run goes on

    # Without the approval too (a state stored before it existed), a used consult with
    # headroom keeps working, and the reason still names the drifting file.
    unapproved = after.model_copy(update={"drift_approved": {}})
    d2 = decide(unapproved, CFG, root=ROOT)
    assert d2.route == "debug" and exit_for(d2) is None
    assert d2.reason == (
        "advisor already consulted on step S4 (one call per step; 2/3 for this run); "
        f"the step continues; plan_drift on {DRIFTED}"
    )


def test_a_stall_or_iteration_exit_still_ends_a_step_whose_consult_is_used() -> None:
    """The debug route after a used consult is no way round the exits: ``verify`` sets the
    exit before triage, and ``decide`` halts on it first."""
    after = _ruled(_state()).model_copy(
        update={
            "last_worker_output": _worker(PLANNED, DRIFTED, confidence="low"),
            "exit_reason": ExitReason.STALL,
        }
    )
    d = decide(after, CFG, root=ROOT)
    assert d.route == "halt" and d.reason == "exit: stall"


def test_a_spent_run_total_still_halts_on_advisor_cap() -> None:
    """Invariant 5: a spent run total halts, with the I-115 wording unchanged."""
    spent = _state(
        budgets=_budget(used=3, on_step={"S1": 1, "S2": 1, "S4": 1}),
        last_worker_output=_worker(PLANNED, DRIFTED, confidence="low"),
    )
    d = decide(spent, CFG, root=ROOT)
    assert d.route == "halt" and exit_for(d) is ExitReason.ADVISOR_CAP
    assert d.reason == f"advisor cap hit (3/3 for this run, step S4); plan_drift on {DRIFTED}"

    # an approved drift is left out of a halt's reason as well
    ruled = spent.model_copy(update={"drift_approved": {"v1:S4": [DRIFTED]}})
    d2 = decide(ruled, CFG, root=ROOT)
    assert d2.route == "halt" and exit_for(d2) is ExitReason.ADVISOR_CAP
    assert d2.reason == "advisor cap hit (3/3 for this run, step S4)"

    # a cap of zero (T1 with no advisor calls configured) is a spent total
    none = _state(budgets=_budget(used=0, cap=0))
    d0 = decide(none, CFG, root=ROOT)
    assert d0.route == "halt" and d0.reason.startswith("advisor cap hit (0/0 for this run")


def test_a_state_without_drift_approvals_still_loads() -> None:
    """Invariant 7: a checkpoint written before the field existed loads, with no approval,
    and behaves as before until its next consult."""
    stored = _state().model_dump(mode="json")
    del stored["drift_approved"]
    loaded = SCIOState.model_validate(stored)
    assert loaded.drift_approved == {}
    d = decide(loaded, CFG, root=ROOT)
    assert d.route == "advise" and d.reason == f"plan_drift; plan_drift on {DRIFTED}"


def test_an_unsettled_design_question_still_halts_for_a_person() -> None:
    """A worker that still needs a design decision after the step's consult is not sent back
    to work: the halt keeps its question in state, and the build parks the feature on it
    (ADR 0020). Only a signal without an open question continues."""
    consulted = _state(budgets=_budget(used=1, on_step={"S3": 1}))
    asking = _worker(PLANNED, confidence="low").model_copy(
        update={"needs_design_decision": True, "design_question": "Keep unicode letters?"}
    )
    after = _ruled(consulted).model_copy(update={"last_worker_output": asking})
    assert not after.budgets.advisor_allowed("S4")

    d = decide(after, CFG, root=ROOT)
    assert d.route == "halt" and exit_for(d) is ExitReason.ADVISOR_CAP
    assert d.reason == "advisor already consulted on step S4 (one call per step; 2/3 for this run)"
    assert after.last_worker_output is not None
    assert after.last_worker_output.design_question == "Keep unicode letters?"
