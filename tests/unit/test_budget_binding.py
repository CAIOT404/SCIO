"""Issue #115: on the Claude subscription the dollar and token budget is a metric, not a stop.

The owner: "th budget is hypaathoical and should no halt scio. it is more of a metic im track
and can use the info later to being optimize sico". With ``SCIO_BUDGET_MODE=auto`` (the
default) a run whose every call goes through Claude Code (``worker_backend`` and
``provider_override`` both ``claude_code``) records its spend and never refuses a call, never
ends on ``budget`` and never caps a Claude Code session; any setup that can bill an API key
keeps its caps. Each negative check is paired with the binding case, so the flag is what
decides the outcome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import ScriptedResponder
from evals.solvers import make_origin
from scio.aci.sandbox.local import LocalSandbox
from scio.graph.report import build_report
from scio.graph.run import run_t0
from scio.graph.run_context import RunContext
from scio.harness.accounting import ModelPricing
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig, budget_binding
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.failures import BudgetExceeded
from scio.harness.providers.fake import FakeProvider
from scio.harness.types import Usage
from scio.loop.claude_code_worker import ClaudeCodeWorker
from scio.loop.exits import LoopLimits, check_exit
from scio.product.build import feature_context
from scio.prompt.registry import PromptRegistry
from scio.settings import Settings
from scio.state.models import Budget, ExitReason, ProgressLedger
from scio.toolchain import python_toolchain
from tests.graph.test_t0_fake import FIXTURE, _spec
from tests.unit.test_claude_code_backend import STREAM, FakeClaude, _result

PRICING = ModelPricing(input_per_mtok=1.0, output_per_mtok=5.0)


def _settings(**kw: Any) -> Settings:
    """Every field ``budget_binding`` reads is set explicitly: CI exports
    ``SCIO_PROVIDER_OVERRIDE=fake``, which a bare ``Settings()`` inherits (I-080)."""
    base: dict[str, Any] = {
        "provider_override": "fake",
        "worker_backend": "aci",
        "budget_mode": "auto",
        "sandbox_backend": "local",
        "gh_repo": None,
    }
    return Settings(**{**base, **kw})


# ---------- the setting ----------


def test_the_budget_mode_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIO_BUDGET_MODE", "track")
    assert Settings().budget_mode == "track"
    monkeypatch.setenv("SCIO_BUDGET_MODE", "enforce")
    assert Settings().budget_mode == "enforce"


def test_enforce_always_binds_and_track_never_does() -> None:
    for worker in ("aci", "claude_code"):
        for provider in (None, "fake", "claude_code"):
            kw = {"worker_backend": worker, "provider_override": provider}
            assert budget_binding(_settings(budget_mode="enforce", **kw)) is True
            assert budget_binding(_settings(budget_mode="track", **kw)) is False


@pytest.mark.parametrize(
    ("worker", "provider", "binds"),
    [
        ("claude_code", "claude_code", False),  # every call on the subscription: a metric
        ("claude_code", None, True),  # the tool-less roles bill an API key
        ("aci", "claude_code", True),  # SCIO's own loop bills an API key
        ("aci", None, True),
    ],
)
def test_auto_tracks_only_when_every_call_runs_on_the_subscription(
    worker: str, provider: str | None, binds: bool
) -> None:
    settings = _settings(budget_mode="auto", worker_backend=worker, provider_override=provider)
    assert budget_binding(settings) is binds


def test_budget_binding_reads_any_settings_like_object_with_safe_defaults() -> None:
    class Bare:
        pass

    assert budget_binding(Bare()) is True  # nothing configured: an API key, so it binds


# ---------- the guard ----------


def test_a_budget_that_does_not_bind_never_refuses_a_call_and_still_records_it() -> None:
    limits = BudgetLimits(tokens_max=100, usd_max=0.001)
    binding = BudgetGuard(limits)
    with pytest.raises(BudgetExceeded):
        binding.reserve(est_input_tokens=10_000, max_output_tokens=10_000, pricing=PRICING)

    guard = BudgetGuard(limits, binding=False)
    guard.reserve(est_input_tokens=10_000, max_output_tokens=10_000, pricing=PRICING)
    guard.record(Usage(input_tokens=5_000, output_tokens=5_000), 2.5)
    guard.reserve(est_input_tokens=10**7, max_output_tokens=10**6, pricing=PRICING)
    snap = guard.snapshot()
    assert snap.tokens_used == 10_000 and snap.usd_used == 2.5 and snap.calls == 1
    assert snap.tokens_max == 100 and snap.usd_max == 0.001  # the caps are still reported
    assert guard.exhausted is False

    binding.record(Usage(input_tokens=5_000, output_tokens=5_000), 2.5)
    assert binding.exhausted is True


def test_tier_guards_carry_the_binding_from_the_settings() -> None:
    track = _settings(budget_mode="track")
    assert BudgetsConfig.for_settings(track).guard_for("T1", binding=False).binding is False
    assert BudgetsConfig.for_settings(track).guard_for("T1").binding is True
    assert Harness.from_settings(track, tier="T1").budget.binding is False
    assert Harness.from_settings(_settings(budget_mode="enforce"), tier="T1").budget.binding


# ---------- the state budget and the exit check ----------


def test_a_state_budget_that_does_not_bind_never_ends_a_run() -> None:
    limits = LoopLimits(max_iters=3, max_stall=1)
    over = Budget(tokens_max=100, usd_max=1.0, advisor_calls_max=0, tokens_used=500, usd_used=9)
    assert over.exhausted and check_exit(ProgressLedger(), over, limits) is ExitReason.BUDGET

    tracked = over.model_copy(update={"binding": False})
    assert tracked.exhausted is False
    assert check_exit(ProgressLedger(), tracked, limits) is None
    # the other runaway guards are unchanged
    assert check_exit(ProgressLedger(iterations=3), tracked, limits) is ExitReason.MAX_ITERS
    assert check_exit(ProgressLedger(stall_count=2), tracked, limits) is ExitReason.STALL


def test_run_and_feature_contexts_carry_the_binding_to_the_state_and_the_guard(
    tmp_path: Path,
) -> None:
    for mode, binds in (("track", False), ("enforce", True)):
        settings = _settings(budget_mode=mode)
        ctx = RunContext.create(
            thread_id=f"t-{mode}",
            sandbox_root=tmp_path / mode,
            tier="T1",
            settings=settings,
            sandbox=LocalSandbox(tmp_path),
            work_dir=tmp_path / f".scio-{mode}",
        )
        try:
            assert ctx.budget.binding is binds and ctx.harness.budget.binding is binds
            ctx.apply_tier_budgets("T2")  # route's resize keeps the binding
            assert ctx.budget.binding is binds and ctx.harness.budget.binding is binds
            feature = feature_context(ctx, f"f-{mode}", "T1")
            assert feature.budget.binding is binds and feature.harness.budget.binding is binds
        finally:
            ctx.close()


# ---------- the Claude Code session cap ----------


@pytest.mark.parametrize("binding", [False, True])
def test_a_claude_code_session_gets_a_dollar_cap_only_when_the_budget_binds(
    tmp_path: Path, binding: bool
) -> None:
    settings = _settings(worker_backend="claude_code")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(
        config,
        ProviderRegistry(config, settings),
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=1_000_000, usd_max=3.0), binding=binding),
    )
    summary = {
        "step_id": "S1", "summary": "done", "files_touched": [], "confidence": "high",
        "needs_design_decision": False, "design_question": None, "next_action": "verify",
        "tests_added": [], "sweep": None,
    }  # fmt: skip
    fake = FakeClaude([STREAM, _result(session_id="sess-w", structured_output=summary)])
    worker = ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("edit_file", "run_tests"), runner=fake,
    )  # fmt: skip
    worker.run("task")
    first = fake.calls[0][0]
    assert ("--max-budget-usd" in first) is binding
    assert "--max-turns" in first  # the turn cap stays in every mode
    assert harness.budget.snapshot().usd_used > 0  # the spend is recorded either way


# ---------- end to end through the graph ----------


@pytest.mark.parametrize("mode", ["track", "enforce"])
def test_a_run_far_over_a_budget_that_does_not_bind_never_ends_on_budget(
    tmp_path: Path, mode: str
) -> None:
    """Mirrors ``tests/graph/test_t0_fake.py::test_budget_exit``: the same $0.0005 cap ends
    the enforced run on ``budget`` at its first model call, while the tracked run goes on to
    the merge pause with its spend recorded far over the cap and reported as tracked."""
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    settings = _settings(
        budget_mode=mode,
        max_attempts=2,
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    ctx = RunContext.create(
        thread_id="t-1",
        sandbox_root=tmp_path / "work",
        settings=settings,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(
            tokens_max=500_000, usd_max=0.0005, advisor_calls_max=0, max_iters=10, max_stall=2
        ),
    )
    ctx.harness.providers.register("fake", FakeProvider(responder=ScriptedResponder()))
    try:
        outcome = run_t0(ctx, _spec(origin))
    finally:
        ctx.close()
    s = outcome.state
    exhausted = [e.text for e in s.task_ledger if "budget exhausted" in e.text]
    if mode == "enforce":
        assert s.exit_reason is ExitReason.BUDGET and exhausted
        return
    assert s.exit_reason is None and outcome.paused and s.phase == "merge_wait"
    assert not exhausted
    assert s.budgets.binding is False
    assert s.budgets.usd_used > s.budgets.usd_max and s.budgets.tokens_used > 0
    line = build_report(s, []).to_markdown()
    assert "(tracked, no cap)" in line and f"${s.budgets.usd_used:.4f}" in line
