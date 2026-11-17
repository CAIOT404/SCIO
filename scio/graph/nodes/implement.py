"""T1/T2 execution nodes: ``implement`` (one step per cycle), ``debug``, ``advise``,
``review``, and the edges around the ``triage`` decision stored by ``verify``.

The capped-session ladder (issue #116, ADR 0005 and 0006 amendments) lives in ``implement``
and ``debug``. A coder or debugger session that ends at its turn cap while git shows no
edit takes the next unused step, cheapest first:

1. Its Claude Code session is resumed once, with tools, for ``RESUME_TURN_CAP`` turns, to
   write the fix its summary describes; the typed summary is taken again. Skipped on the
   ``aci`` backend, whose turns have no session.
2. The next session gets the previous one's findings (I-154, ``previous_on_step``).
3. If it still edited nothing, the next session on the same plan step gets
   ``RAISED_CAP_FACTOR`` times the turn cap, once per step, and this iteration counts no
   stall (``ProgressLedger.stall_exempt``).
4. Once the raised cap is spent, a capped session that edited nothing counts toward a stall
   as before.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from scio.aci.sandbox.base import Sandbox
from scio.aci.toolbox import ToolBox
from scio.context.projections import advisor_brief, debug_message, review_message, step_message
from scio.graph.nodes.common import (
    changed_since_head,
    decision,
    diff_stat,
    fact,
    git_out,
    untracked_files,
)
from scio.graph.nodes.planning import keep_localized_files, normalize_steps, summaries_text
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.loop.agent_loop import LoopResult
from scio.loop.backends import (
    RAISED_CAP_FACTOR,
    RESUME_TURN_CAP,
    ResumableWorker,
    Worker,
    make_worker,
)
from scio.loop.escalation import approve_drift
from scio.loop.impact import assess
from scio.state.models import (
    AdvisorOutput,
    EscalationRecord,
    EscalationTrigger,
    ExitReason,
    LedgerEntry,
    Plan,
    ReviewerOutput,
    Role,
    SCIOState,
    Verdict,
    WorkerOutput,
)

Node = Callable[[SCIOState], dict[str, Any]]
WRITER_TOOLS = (
    "repo_search",
    "view_file",
    "edit_file",
    "delete_file",
    "run_tests",
    "install_deps",
    "run_app",
)


def _diff_text(ctx: RunContext, state: SCIOState) -> str:
    """The full diff since the base commit, new files included (``git add -N``, I-063)."""
    base = state.github.base_sha
    new = untracked_files(ctx.sandbox)
    if new:
        ctx.sandbox.exec(["git", "add", "-N", "--", *new], timeout_s=60)
    return git_out(ctx.sandbox, "diff", base) if base else git_out(ctx.sandbox, "diff")


CAPPED_STOPS = frozenset({"tool_cap", "model_cap"})
"""Stops that mean a session ran out of turns: Claude Code's ``--max-turns`` and SCIO's tool
cap report ``tool_cap``; SCIO's own loop on T1/T2 stops at its model-call cap,
``model_cap``."""

RESUME_PROMPT = (
    "Your turn cap ended this session before you changed any file, and your JSON summary "
    "above records what you found. You have diagnosed it; now write the fix: make the edits "
    "your summary describes, run the step's tests, and stop when they pass."
)
"""Ladder step 1's prompt on the resumed session (issue #116)."""

SNAPSHOT_INDEX = "scio-snapshot.index"
"""A throwaway git index in the sandbox's git directory, for :func:`tree_snapshot`."""


def tree_snapshot(sandbox: Sandbox) -> str | None:
    """The id of a git tree of every file in the working tree that git does not ignore,
    written through a throwaway index so the repository's own index is untouched.

    Equal ids before and after a worker session mean git saw no edit (issue #116), whatever
    the session reports. ``changed_since_head`` cannot tell: an earlier session's
    uncommitted edits on the same step are still in the tree. None when git cannot say.
    """
    gitdir = sandbox.exec(["git", "rev-parse", "--absolute-git-dir"], timeout_s=60)
    if not gitdir.ok:
        return None
    env = {"GIT_INDEX_FILE": f"{gitdir.stdout.strip()}/{SNAPSHOT_INDEX}"}
    added = sandbox.exec(["git", "add", "--all", "--", "."], env=env, timeout_s=120)
    if not added.ok:
        return None
    tree = sandbox.exec(["git", "write-tree"], env=env, timeout_s=60)
    return (tree.stdout.strip() or None) if tree.ok else None


def _edited(before: str | None, after: str | None) -> bool | None:
    return None if before is None or after is None else before != after


@dataclass
class Session:
    """One coder or debugger session as the capped-session ladder sees it (issue #116)."""

    result: LoopResult
    """The session's result; after a resume, merged with the resume's (its output and stop)."""
    edited: bool | None
    """Whether git saw the tree change over the session and its resume; None: git cannot say."""
    capped: LoopResult | None = None
    """The first call's result when the session was resumed (ladder step 1)."""
    raised: bool = False
    """The session ran with the raised turn cap (ladder step 3)."""


def ladder_key(state: SCIOState) -> str | None:
    """The plan step a raised turn cap is granted to: plan version and step id, since a
    replan's steps reuse ids (``S1``)."""
    step_id = state.progress.current_step_id
    if state.plan is None or step_id is None:
        return None
    return f"v{state.plan.version}:{step_id}"


def raised_cap_due(state: SCIOState) -> bool:
    """Ladder step 3: this session is the one the step was granted the raised cap for."""
    p, key = state.progress, ladder_key(state)
    return p.raised_cap_pending and key is not None and p.raised_cap_step == key


def resume_if_capped(
    ctx: RunContext,
    loop: Worker,
    first: LoopResult,
    before: str | None,
    *,
    tags: dict[str, str],
    raised: bool,
) -> Session:
    """Ladder step 1 after a session's first call (``before`` is the tree snapshot taken
    before it): resume the session once when it ran out of turns and git shows no edit. The
    resume continues the same Claude Code session with tools for ``RESUME_TURN_CAP`` turns
    and takes its typed summary again; ``AgentLoop`` has no session (``session_id`` None), so
    it is never resumed. Called inside the node's ``BudgetExceeded`` guard (I-098)."""
    edited = _edited(before, tree_snapshot(ctx.sandbox))
    if (
        first.stop not in CAPPED_STOPS
        or edited is not False
        or not first.session_id
        or not isinstance(loop, ResumableWorker)
    ):
        return Session(result=first, edited=edited, raised=raised)
    second = loop.resume_typed(
        first.session_id, RESUME_PROMPT, WorkerOutput, max_turns=RESUME_TURN_CAP, tags=tags
    )
    merged = LoopResult(
        output=second.output,
        stop=second.stop,
        tool_calls=first.tool_calls + second.tool_calls,
        model_calls=first.model_calls + second.model_calls,
        messages=[*first.messages, *second.messages],
        session_id=second.session_id,
    )
    return Session(
        result=merged,
        edited=_edited(before, tree_snapshot(ctx.sandbox)),
        capped=first,
        raised=raised,
    )


def _ladder(
    state: SCIOState, session: Session, *, role: Role, step_id: str | None
) -> tuple[dict[str, Any], list[LedgerEntry]]:
    """The progress fields and ledger facts the ladder writes after a session."""
    stop = session.result.stop
    key = ladder_key(state)
    capped_empty = stop in CAPPED_STOPS and session.edited is False
    grant = capped_empty and key is not None and state.progress.raised_cap_step != key
    entries: list[LedgerEntry] = []
    if session.capped is not None:
        first = session.capped.output
        found = first.summary if isinstance(first, WorkerOutput) else ""
        entries.append(
            fact(
                f"ladder step 1 on {step_id}: the {role.value} session ended at its turn cap "
                f"({session.capped.stop}) with no edit in git and was resumed once for up to "
                f"{RESUME_TURN_CAP} turns; "
                + ("it edited" if session.edited else "it still edited nothing")
                + (f". Its first summary: {found}" if found else ""),
                source=Role.SYSTEM,
                step_id=step_id,
            )
        )
    if grant:
        entries.append(
            fact(
                f"ladder step 3 on {step_id}: the {role.value} session ended at its turn cap "
                f"with no edit; the next session on this step gets {RAISED_CAP_FACTOR}x the "
                "turn cap, and this iteration counts no stall",
                source=Role.SYSTEM,
                step_id=step_id,
            )
        )
    elif session.raised:
        entries.append(
            fact(
                f"ladder step 3 on {step_id}: the {role.value} session ran with "
                f"{RAISED_CAP_FACTOR}x the turn cap"
                + (
                    "; it ended at its cap with no edit, so stalls count as before (step 4)"
                    if capped_empty
                    else ""
                ),
                source=Role.SYSTEM,
                step_id=step_id,
            )
        )
    fields: dict[str, Any] = {
        "last_worker_stop": stop,
        "last_worker_edited": session.edited,
        "last_worker_resumed": session.capped is not None,
        "raised_cap_step": key if grant else state.progress.raised_cap_step,
        "raised_cap_pending": grant,
        "stall_exempt": grant,
    }
    return fields, entries


def _after_worker(
    ctx: RunContext, state: SCIOState, session: Session, *, role: Role
) -> dict[str, Any]:
    result = session.result
    output: WorkerOutput = result.output
    tool_calls, stop = result.tool_calls, result.stop
    attempt = state.progress.attempts_on_step + 1
    ref = ctx.artifacts.put(
        "log",
        result.transcript(),
        produced_by=role,
        name=f"{role.value}-{state.progress.iterations + 1:02d}",
    )
    # The plan, not the model, says which step is current: a worker that names a finished
    # step would otherwise move the run back onto it and loop (I-046).
    step_id = state.progress.current_step_id or output.step_id
    ladder, ladder_entries = _ladder(state, session, role=role, step_id=step_id)
    progress = state.progress.model_copy(
        update={
            "current_step_id": step_id,
            "attempts_on_step": attempt,
            "tool_calls": state.progress.tool_calls + tool_calls,
            **ladder,
        }
    )
    flags = assess(
        _diff_text(ctx, state),
        max_files=ctx.escalation.high_impact_files,
        toolchain=ctx.toolchain,
    )
    entries = [
        fact(
            f"{role.value} attempt {attempt} on {progress.current_step_id}: {output.summary} "
            f"[{tool_calls} tool calls, stop={stop}, confidence={output.confidence}]",
            source=role,
            step_id=progress.current_step_id,
        ),
        *ladder_entries,
    ]
    if output.sweep is not None:
        entries.append(
            fact(
                f"sweep '{output.sweep.pattern}': fixed {output.sweep.files_fixed}, "
                f"ruled out {output.sweep.ruled_out}",
                source=role,
                step_id=progress.current_step_id,
            )
        )
    return {
        "last_worker_output": output,
        "progress": progress,
        "artifacts": {**state.artifacts, f"{role.value}:{progress.iterations + 1}": ref},
        "high_impact_flags": flags,
        "advisor_guidance": None,
        "task_ledger": entries,
        "phase": "verify",
        **ctx.budget_update(),
    }


def previous_on_step(state: SCIOState) -> WorkerOutput | None:
    """The last worker session's report when it worked on the current step: each coder or
    debugger iteration is a fresh session, so without it a session that only investigated
    passes nothing on and the next repeats its reconnaissance (I-154). A report about an
    earlier, committed step is not carried."""
    w, step = state.last_worker_output, state.current_step()
    return w if w is not None and step is not None and w.step_id == step.id else None


def implement(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        step = state.current_step()
        if step is None:
            entry = fact("implement: no open plan step", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.ESCALATE_HUMAN, "task_ledger": [entry]}
        last_ref = state.artifacts.get("verify:last")
        last_verify = ctx.artifacts.head(last_ref, 3000) if last_ref else None
        toolbox = ToolBox(
            ctx.sandbox, WRITER_TOOLS, human_gate=False, role="coder", context=ctx.tool_context
        )
        raised = raised_cap_due(state)
        loop = make_worker(ctx, toolbox, "coder", cap_factor=RAISED_CAP_FACTOR if raised else 1)
        previous = previous_on_step(state)
        msg = step_message(
            state.task,
            step,
            state.step_features(),
            repo_root=ctx.sandbox.root,
            attempt=state.progress.attempts_on_step + 1,
            summaries=summaries_text(ctx, state),
            guidance=state.advisor_guidance,
            last_verify=last_verify,
            previous=previous,
            tree=diff_stat(ctx.sandbox, "HEAD") if previous is not None else None,
        )
        tags = {"thread": ctx.thread_id, "step": step.id}
        before = tree_snapshot(ctx.sandbox)
        try:
            first = loop.run(msg, tags=tags)
            session = resume_if_capped(ctx, loop, first, before, tags=tags, raised=raised)
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted during implement: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        return _after_worker(ctx, state, session, role=Role.CODER)

    return node


def debug(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        last_ref = state.artifacts.get("verify:last")
        failing = ctx.artifacts.head(last_ref, 4000) if last_ref else "(no verify output)"
        toolbox = ToolBox(
            ctx.sandbox, WRITER_TOOLS, human_gate=False, role="debugger", context=ctx.tool_context
        )
        raised = raised_cap_due(state)
        loop = make_worker(ctx, toolbox, "debugger", cap_factor=RAISED_CAP_FACTOR if raised else 1)
        previous = previous_on_step(state)
        msg = debug_message(
            state.current_step(),
            repo_root=ctx.sandbox.root,
            failing_output=failing,
            signature=state.progress.last_error_signature,
            attempts=state.progress.fix_attempts_on_error,
            guidance=state.advisor_guidance,
            previous=previous,
            tree=diff_stat(ctx.sandbox, "HEAD") if previous is not None else None,
            attempt=state.progress.attempts_on_step + 1,
            task=state.task,
        )
        tags = {"thread": ctx.thread_id, "debug": str(state.progress.iterations)}
        before = tree_snapshot(ctx.sandbox)
        try:
            first = loop.run(msg, tags=tags)
            session = resume_if_capped(ctx, loop, first, before, tags=tags, raised=raised)
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted during debug: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        return _after_worker(ctx, state, session, role=Role.DEBUGGER)

    return node


def after_worker(state: SCIOState) -> Literal["verify", "halt"]:
    return "halt" if state.exit_reason is not None else "verify"


def triage(state: SCIOState) -> Literal["debug", "advise", "halt"]:
    """Pure edge: reads the decision ``verify`` stored."""
    if state.exit_reason is not None:
        return "halt"
    return state.triage.route if state.triage is not None else "debug"


def advise(ctx: RunContext) -> Node:
    """Consult the Opus advisor with a compact brief; its output is the authoritative revision."""

    def node(state: SCIOState) -> dict[str, Any]:
        triggers = state.triage.triggers if state.triage else [EscalationTrigger.LOW_CONFIDENCE]
        step = state.current_step()
        step_id = step.id if step else (state.progress.current_step_id or "S?")
        last_ref = state.artifacts.get("verify:last")
        last_error = ctx.artifacts.head(last_ref, 1500) if last_ref else "(none)"
        verify_passed = state.progress.last_failure_kind == "none"  # ADR 0008 review
        brief = advisor_brief(
            state.task,
            step,
            triggers=triggers,
            diff_stat=diff_stat(ctx.sandbox, state.github.base_sha),
            last_error=last_error,
            attempts=state.progress.fix_attempts_on_error,
            worker_summary=state.last_worker_output.summary if state.last_worker_output else None,
            impact_flags=state.high_impact_flags,
            verify_passed=verify_passed,
        )
        brief_ref = ctx.artifacts.put(
            "brief", brief, produced_by=Role.SYSTEM, name=f"brief-{len(state.escalations) + 1:02d}"
        )
        before = ctx.harness.ledger.totals()
        req = ctx.harness.build_request(
            "advisor",
            system=ctx.prompts.get("advisor").render(),
            messages=[Message.user(brief)],
            tags={
                "thread": ctx.thread_id,
                "step": step_id,
                "trigger": ",".join(t.value for t in triggers),
            },
        )
        record = EscalationRecord(
            id=f"E{len(state.escalations) + 1}-{uuid.uuid4().hex[:6]}",
            trigger=triggers[0],
            step_id=step_id,
            brief=brief_ref,
        )
        try:
            out: AdvisorOutput = ctx.harness.structured(req, AdvisorOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at advise: {exc.message}", source=Role.SYSTEM)
            return {
                "exit_reason": ExitReason.BUDGET,
                "escalations": [*state.escalations, record],
                "task_ledger": [entry],
                **ctx.budget_update(),
            }
        after = ctx.harness.ledger.totals()
        record = record.model_copy(
            update={
                "verdict": out.verdict,
                "tokens": after.usage.total - before.usage.total,
                "usd": after.usd - before.usd,
                "outcome": "pending",
            }
        )
        budgets = ctx.budget.model_copy(
            update={
                "advisor_calls_used": state.budgets.advisor_calls_used + 1,
                "advisor_calls_on_step": {
                    **state.budgets.advisor_calls_on_step,
                    step_id: state.budgets.advisor_calls_on_step.get(step_id, 0) + 1,
                },
            }
        )
        ctx.budget = budgets
        patch: dict[str, Any] = {
            "escalations": [*state.escalations, record],
            "advisor_guidance": out.guidance,
            "high_impact_reviewed": True,
            "sample_disagreement": False,
            "triage": None,
            "task_ledger": [
                decision(
                    f"advisor ({record.trigger.value}) verdict {out.verdict.value}: "
                    f"{out.guidance[:300]}",
                    source=Role.ADVISOR,
                    step_id=step_id,
                )
            ],
        }
        if out.verdict is Verdict.REPLAN and out.revised_steps and state.plan is not None:
            done_ids = {s.id for s in state.plan.steps if s.done}
            steps = [s.model_copy(update={"done": s.id in done_ids}) for s in out.revised_steps]
            steps, plan_notes = normalize_steps(steps, [f.id for f in state.feature_list])
            revised, localized = keep_localized_files(  # the advisor names files anew (I-151)
                ctx,
                state,
                Plan(
                    version=state.plan.version + 1,
                    author=Role.ADVISOR,
                    steps=steps,
                    rationale=out.guidance,
                    revision_reason=f"advisor replan after {record.trigger.value}",
                ),
            )
            steps = revised.steps
            patch["task_ledger"] = patch["task_ledger"] + [
                fact(n, source=Role.SYSTEM) for n in [*plan_notes, *localized]
            ]
            patch["plan"] = revised
            first_open = next((s.id for s in steps if not s.done), None)
            patch["progress"] = state.progress.model_copy(
                update={
                    "current_step_id": first_open,
                    "attempts_on_step": 0,
                    "fix_attempts_on_error": 0,
                }
            )
            patch["phase"] = "implement"
        elif out.verdict is Verdict.REPLAN:
            patch["phase"] = "plan"
        elif out.verdict is Verdict.ESCALATE_HUMAN:
            patch["exit_reason"] = ExitReason.ESCALATE_HUMAN
            patch["phase"] = "halted"
        elif out.verdict is Verdict.CONTINUE and verify_passed:
            # ADR 0008: a pre-commit review that approves the diff has nothing to resolve;
            # the run commits without another coder turn.
            record = record.model_copy(update={"outcome": "resolved"})
            patch["escalations"] = [*state.escalations, record]
            patch["phase"] = "checkpoint"
        else:
            patch["phase"] = "implement"
        if out.verdict is Verdict.CONTINUE and EscalationTrigger.PLAN_DRIFT in triggers:
            # Issue #172: the advisor ruled on this drift and said continue, so the same
            # files do not fire again on this step. Git names them, as in verify (I-166);
            # no other verdict approves anything. Written with the verdict, in one update.
            patch["drift_approved"] = approve_drift(
                state,
                ctx.escalation,
                verdict=out.verdict,
                triggers=triggers,
                touched=changed_since_head(ctx.sandbox),
                root=ctx.sandbox.root,
            )
        patch.update(ctx.budget_update())
        patch["budgets"] = patch["budgets"].model_copy(
            update={
                "advisor_calls_used": budgets.advisor_calls_used,
                "advisor_calls_on_step": budgets.advisor_calls_on_step,
            }
        )
        ctx.budget = patch["budgets"]
        return patch

    return node


def after_advise(state: SCIOState) -> Literal["implement", "plan", "checkpoint", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    if state.phase == "checkpoint":  # ADR 0008: approved pre-commit review
        return "checkpoint"
    return "plan" if state.phase == "plan" else "implement"


def review(ctx: RunContext) -> Node:
    """Fresh-context Reviewer: task + diff + test output only. Advisory."""

    def node(state: SCIOState) -> dict[str, Any]:
        diff_text = _diff_text(ctx, state)
        diff_ref = ctx.artifacts.put("diff", diff_text, produced_by=Role.SYSTEM, name="diff-final")
        last_ref = state.artifacts.get("verify:last")
        test_out = ctx.artifacts.head(last_ref, 3000) if last_ref else "(no verify output)"
        req = ctx.harness.build_request(
            "reviewer",
            system=ctx.prompts.get("reviewer").render(),
            messages=[
                Message.user(review_message(state.task, diff_text=diff_text, test_output=test_out))
            ],
            tags={"thread": ctx.thread_id},
        )
        try:
            out: ReviewerOutput = ctx.harness.structured(req, ReviewerOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at review: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        blockers = [f for f in out.findings if f.severity == "blocker"]
        entry = fact(
            f"review {out.overall}: {len(out.findings)} finding(s), {len(blockers)} blocker(s)",
            source=Role.REVIEWER,
        )
        return {
            "last_review": out,
            "artifacts": {**state.artifacts, "diff:final": diff_ref},
            "task_ledger": [entry],
            "phase": "document",
            **ctx.budget_update(),
        }

    return node


def after_review(state: SCIOState) -> Literal["document", "halt"]:
    return "halt" if state.exit_reason is not None else "document"
