"""The escalation policy: six signals, a never-escalate list, and the caps.

Pure functions of state. ``decide`` returns a :class:`TriageDecision` that the ``verify``
node stores and the ``triage`` conditional edge reads, so routing never computes.

Signals (any one escalates):
  1. repeated error: the same error signature seen ``repeat_threshold`` times
  2. stall: ``stall_count`` above ``max_stall``
  3. the worker reported ``confidence=low`` or ``needs_design_decision``
  4. plan drift: the diff touched files outside the step's ``files_expected``
  5. sampled patches disagreed and none passed
  6. high-impact change (public API, schema, dependencies, > N files)

Never escalate for: lint/format-only failures, a first clear test failure (the default
path is ``debug``), docs, schema repairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from scio.state.models import EscalationTrigger, ExitReason, SCIOState, TriageDecision, Verdict


@dataclass(frozen=True)
class EscalationConfig:
    repeat_threshold: int = 3
    max_stall: int = 2
    high_impact_files: int = 8
    sample_n: int = 1
    explorers_min: int = 2
    explorers_max: int = 4
    test_dirs: tuple[str, ...] = ("tests/", "test/")

    def __post_init__(self) -> None:
        # ADR 0006: the stall exit must not fire before the repeated-error signal can escalate.
        if self.max_stall < self.repeat_threshold - 1:
            raise ValueError(
                f"max_stall ({self.max_stall}) must be >= repeat_threshold - 1 "
                f"({self.repeat_threshold - 1}); see ADR 0006"
            )


def _norm(path: str, root: str | None) -> str:
    p = path.replace("\\", "/")
    if root:
        r = root.replace("\\", "/").rstrip("/") + "/"
        if p.startswith(r):
            p = p[len(r) :]
    # a leading "./" or "/" goes, never the dot of a dot-directory (I-160: `lstrip("./")`
    # turned `.github/workflows/ci.yml` into `github/workflows/ci.yml`)
    while p.startswith(("./", "/")):
        p = p[2:] if p.startswith("./") else p[1:]
    return p


def drifted_files(
    touched: list[str], expected: list[str], *, root: str | None, test_dirs: tuple[str, ...]
) -> list[str]:
    """Touched files that are neither expected by the plan step nor test files."""
    exp = [_norm(e, root) for e in expected]
    out: list[str] = []
    for raw in touched:
        t = _norm(raw, root)
        if any(t.startswith(d) for d in test_dirs) or PurePosixPath(t).name.startswith("test_"):
            continue
        if any(t == e or t.startswith(e.rstrip("/") + "/") for e in exp):
            continue
        out.append(t)
    return out


def approval_key(state: SCIOState) -> str | None:
    """The key of the current plan step in ``SCIOState.drift_approved``: plan version and
    step id, since a replan's steps reuse ids (``S1``) and must start with no approval."""
    step_id = state.progress.current_step_id
    if state.plan is None or step_id is None:
        return None
    return f"v{state.plan.version}:{step_id}"


def open_drift(state: SCIOState, cfg: EscalationConfig, *, root: str | None = None) -> list[str]:
    """The files that drift from the current step's plan and that no advisor ``continue``
    ruled on for this step (issue #172). Empty when the step names no files: there is no
    plan to drift from."""
    step, w = state.current_step(), state.last_worker_output
    if w is None or step is None or not step.files_expected:
        return []
    files = drifted_files(w.files_touched, step.files_expected, root=root, test_dirs=cfg.test_dirs)
    key = approval_key(state)
    approved = set(state.drift_approved.get(key, [])) if key is not None else set()
    return [f for f in files if f not in approved]


def approve_drift(
    state: SCIOState,
    cfg: EscalationConfig,
    *,
    verdict: Verdict,
    triggers: list[EscalationTrigger],
    touched: list[str],
    root: str | None = None,
) -> dict[str, list[str]]:
    """``drift_approved`` after an advisor consult (issue #172). Only a ``continue`` on a
    consult whose triggers included ``plan_drift`` approves: the current step's entry gains
    the files of ``touched`` that drift from its plan. ``replan``, ``change_approach`` and
    ``escalate_human`` record nothing. A pure function; the ``advise`` node stores it."""
    if verdict is not Verdict.CONTINUE or EscalationTrigger.PLAN_DRIFT not in triggers:
        return state.drift_approved
    step, key = state.current_step(), approval_key(state)
    if step is None or key is None or not step.files_expected:
        return state.drift_approved
    files = drifted_files(touched, step.files_expected, root=root, test_dirs=cfg.test_dirs)
    if not files:
        return state.drift_approved
    merged = list(dict.fromkeys([*state.drift_approved.get(key, []), *files]))
    return {**state.drift_approved, key: merged}


def detect(
    state: SCIOState, cfg: EscalationConfig, *, root: str | None = None
) -> list[EscalationTrigger]:
    p = state.progress
    found: list[EscalationTrigger] = []
    if p.fix_attempts_on_error >= cfg.repeat_threshold:
        found.append(EscalationTrigger.REPEATED_ERROR)
    if p.stall_count > cfg.max_stall:
        found.append(EscalationTrigger.STALL)
    w = state.last_worker_output
    if w is not None and (w.confidence == "low" or w.needs_design_decision):
        found.append(EscalationTrigger.LOW_CONFIDENCE)
    if open_drift(state, cfg, root=root):  # an approved drift does not fire again (#172)
        found.append(EscalationTrigger.PLAN_DRIFT)
    if state.sample_disagreement:
        found.append(EscalationTrigger.SAMPLE_DISAGREEMENT)
    if state.high_impact_flags and not state.high_impact_reviewed:
        found.append(EscalationTrigger.HIGH_IMPACT)
    return found


def decide(state: SCIOState, cfg: EscalationConfig, *, root: str | None = None) -> TriageDecision:
    """Where a failing verify goes next. Only meaningful for tiers with a plan (T1/T2)."""
    if state.exit_reason is not None:
        return TriageDecision(route="halt", reason=f"exit: {state.exit_reason.value}")
    if state.progress.last_failure_kind == "lint_only":
        return TriageDecision(route="debug", reason="never escalate: lint/format only")
    if state.progress.last_failure_kind == "quality":
        return TriageDecision(route="debug", reason="never escalate: product quality check only")
    triggers = detect(state, cfg, root=root)
    if not triggers:
        return TriageDecision(route="debug", reason="no escalation signal; clear failure path")
    step_id = state.progress.current_step_id or "S?"
    # I-166: a drift signal says which files, so a halt can be checked against the plan.
    # Only the files still drifting: an advisor ``continue`` settled the others (#172).
    drift = ""
    if EscalationTrigger.PLAN_DRIFT in triggers:
        files = open_drift(state, cfg, root=root)
        drift = f"; plan_drift on {', '.join(files[:8])}" + (" ..." if len(files) > 8 else "")
    if state.budgets.advisor_allowed(step_id):
        return TriageDecision(
            route="advise",
            triggers=triggers,
            reason=", ".join(t.value for t in triggers) + drift,
        )
    used, cap = state.budgets.advisor_calls_used, state.budgets.advisor_calls_max
    # I-115: name the cap that fired. The per-step cap (one call per step) refuses while the
    # run's total still has headroom, and "2/3" alone reads as headroom left. The total wins
    # when both are spent, and only a spent total halts.
    if used >= cap:
        reason = f"advisor cap hit ({used}/{cap} for this run, step {step_id})"
        return TriageDecision(route="halt", triggers=triggers, reason=reason + drift)
    reason = (
        f"advisor already consulted on step {step_id} "
        f"(one call per step; {used}/{cap} for this run)"
    )
    w = state.last_worker_output
    if w is not None and w.needs_design_decision:
        # A design question the step's consult did not settle goes to a person, as before:
        # the halt keeps the worker's question in state, and the build parks the feature on
        # it (ADR 0020). Sending it back to work would lose the question to the next turn.
        return TriageDecision(route="halt", triggers=triggers, reason=reason + drift)
    # Issue #172: the step's one consult is used and the run's total is not. The step goes
    # back to work instead of halting at once: this is an ordinary fix attempt, so the
    # stall and iteration exits (checked before this function runs) still end a step that
    # makes no progress. The triggers stay on the decision for the ledger.
    return TriageDecision(
        route="debug", triggers=triggers, reason=reason + "; the step continues" + drift
    )


def review_decision(state: SCIOState, cfg: EscalationConfig) -> TriageDecision | None:
    """Where a *passing* verify goes when the diff is high impact (ADR 0008).

    A diff that trips the impact detector (more than N files, dependency or schema files, a
    removed public symbol) gets one advisor review before it is committed, if the caps
    allow. Nothing else about a passing verify can escalate; a refused review commits as
    usual, which is why this returns None instead of a halt.
    """
    if state.exit_reason is not None or state.high_impact_reviewed or not state.high_impact_flags:
        return None
    step_id = state.progress.current_step_id or "S?"
    if not state.budgets.advisor_allowed(step_id):
        return None
    return TriageDecision(
        route="advise",
        triggers=[EscalationTrigger.HIGH_IMPACT],
        reason="high-impact diff reviewed before commit: " + "; ".join(state.high_impact_flags),
    )


def exit_for(decision: TriageDecision) -> ExitReason | None:
    """The exit reason a ``halt`` triage implies (set by the node that stores the decision)."""
    if decision.route == "halt" and decision.triggers:
        return ExitReason.ADVISOR_CAP
    return None
