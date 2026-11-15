"""Issue #140 (graphcalc M4): the owner answered ``skip`` for P29, P30 passed and the
integration checks were green, yet the milestone gate stopped the build again with
``escalate_human``: "P29 was skipped and needs a human decision". The skip's own question
promised "this milestone goes on without it" (I-116). A feature the owner skipped in this
milestone is deferred, not undelivered: the gate's input names it as deferred and leaves its
criteria out, and the code never forces an escalation because of it. A feature that failed,
never ran, or stopped without a decision still stops the gate (I-230)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.fake_scripts import ScriptedResponder
from scio.graph.run_context import RunContext
from scio.harness.types import CompletionRequest
from scio.product.build import MILESTONE_GATE_HEADER, milestone_gate
from scio.state.models import (
    FeatureRun,
    MilestoneFeatureOutput,
    MilestonePlan,
    ProductBuildState,
    ProductFeature,
    ProductSpec,
    Tier,
)
from tests.graph.test_product_build_fake import PRODUCT, _ctx, _harness, _settings

MID = "M4"
CRITERIA = {
    "P27": "criterion-p27-tokens-are-lexed",
    "P28": "criterion-p28-errors-name-the-column",
    "P29": "criterion-p29-accepts-dot-five-and-five-dot",
    "P30": "criterion-p30-history-is-kept",
}


class _Recording(ScriptedResponder):
    """The scripted milestone gate, keeping the text of every milestone gate request."""

    def __init__(self, verdict: str = "pass") -> None:
        super().__init__(milestone_gate_verdict=verdict)
        self.gate_inputs: list[str] = []

    def __call__(self, req: CompletionRequest) -> Any:
        text = "\n".join(m.text() for m in req.messages)
        if text.startswith(MILESTONE_GATE_HEADER):
            self.gate_inputs.append(text)
        return super().__call__(req)


def _run(fid: str, status: str, **extra: Any) -> FeatureRun:
    return FeatureRun(
        feature_id=fid,
        thread_id=f"build-graphcalc-m4-{fid.lower()}-a1",
        tier=Tier.T1,
        status=status,  # type: ignore[arg-type]
        milestone=extra.pop("milestone", MID),
        **extra,
    )


def _state(ctx: RunContext, runs: list[FeatureRun], ids: list[str]) -> ProductBuildState:
    spec = ProductSpec(
        name=PRODUCT,
        title="graphcalc",
        vision="v",
        users=["u"],
        features=[
            ProductFeature(id=fid, description=f"feature {fid}", acceptance=[CRITERIA[fid]])
            for fid in CRITERIA
        ],
        quality_bar=["q"],
    )
    plan = MilestonePlan(
        number=4,
        title="expressions",
        branch="milestone/4",
        features=[MilestoneFeatureOutput(id=fid) for fid in ids],
    )
    assert plan.id == MID
    return ProductBuildState(
        thread_id=ctx.thread_id,
        product_dir=str(ctx.sandbox.root),
        spec=spec,
        milestone=plan,
        feature_runs=runs,
        integration="[lint] clean\n[tests] passed",
        integration_ok=True,
        budgets=ctx.budget,
    )


def _owner_skip(fid: str) -> FeatureRun:
    """The run ``_feature_decision`` records when the owner answers ``skip``."""
    return _run(fid, "skipped", exit_reason="escalate_human", decision="skip")


def test_a_skipped_feature_does_not_stop_the_milestone_gate(tmp_path: Path) -> None:
    """Invariant 1: a feature the human skipped in this milestone counts as deferred, not
    undelivered. With green checks and a passing verdict, the milestone goes on to its PR."""
    harness = _harness(_settings(tmp_path), _Recording("pass"))
    ctx = _ctx(tmp_path, harness, "build-graphcalc-m4")
    try:
        runs = [
            _run("P27", "passed", commit_sha="a" * 40),
            _run("P28", "passed", commit_sha="b" * 40),
            _owner_skip("P29"),
            _run("P30", "passed", commit_sha="c" * 40),
        ]
        patch = milestone_gate(ctx)(_state(ctx, runs, ["P27", "P28", "P29", "P30"]))
        gate = patch["last_gate"]
        assert gate.verdict == "pass", gate.reasoning
        assert not gate.reasoning.startswith("overridden"), gate.reasoning
        assert patch["phase"] == "pr"
    finally:
        ctx.close()


def test_the_gate_input_names_a_skipped_feature_as_deferred(tmp_path: Path) -> None:
    """Invariant 2: the gate judges the features that were built. A deferred feature is named
    as deferred by the owner, and its criteria are not among the ones the gate must judge."""
    responder = _Recording("pass")
    harness = _harness(_settings(tmp_path), responder)
    ctx = _ctx(tmp_path, harness, "build-graphcalc-m4")
    try:
        runs = [
            _run("P27", "passed", commit_sha="a" * 40),
            _run("P28", "passed", commit_sha="b" * 40),
            _owner_skip("P29"),
            _run("P30", "passed", commit_sha="c" * 40),
        ]
        milestone_gate(ctx)(_state(ctx, runs, ["P27", "P28", "P29", "P30"]))
        (text,) = responder.gate_inputs
        # the built features are judged, each with its criteria
        for fid in ("P27", "P28", "P30"):
            assert f"### {fid}: feature {fid}" in text
            assert f"- {CRITERIA[fid]}" in text
        # the deferred one is named as deferred by the owner, with no criteria to judge
        assert "### P29" not in text
        assert CRITERIA["P29"] not in text
        assert "## Deferred by the owner" in text
        assert "P29: feature P29" in text.split("## Deferred by the owner", 1)[1]
    finally:
        ctx.close()


def test_a_failed_or_unrun_feature_still_stops_the_gate(tmp_path: Path) -> None:
    """Invariant 3: a feature that failed, never ran, or stopped without a decision still
    stops the gate as today, and the override names exactly those, never a deferred one."""
    harness = _harness(_settings(tmp_path), _Recording("pass"))
    ctx = _ctx(tmp_path, harness, "build-graphcalc-m4")
    node = milestone_gate(ctx)
    try:
        cases: list[tuple[str, FeatureRun | None]] = [
            ("failed and stopped", _run("P28", "failed", decision="stop")),
            ("failed, no one asked", _run("P28", "failed", exit_reason="error")),
            ("never ran", None),
            ("skipped without a decision", _run("P28", "skipped")),
            ("skipped in an earlier milestone", _owner_skip("P28").model_copy(
                update={"milestone": "M3"}
            )),
        ]  # fmt: skip
        for label, p28 in cases:
            runs = [_run("P27", "passed", commit_sha="a" * 40), _owner_skip("P29")]
            if p28 is not None:
                runs.insert(1, p28)
            patch = node(_state(ctx, runs, ["P27", "P28", "P29"]))
            gate = patch["last_gate"]
            assert gate.verdict == "escalate_human", label
            assert gate.objective_met is False, label
            assert patch["phase"] == "gate", label
            assert "features not passed: P28." in gate.reasoning, (label, gate.reasoning)
            assert "P29" not in gate.reasoning.split(". ", 1)[0], (label, gate.reasoning)
    finally:
        ctx.close()


def test_a_milestone_whose_every_feature_was_skipped_still_stops(tmp_path: Path) -> None:
    """Nothing was delivered: a passing verdict is still overridden, as before #140."""
    harness = _harness(_settings(tmp_path), _Recording("pass"))
    ctx = _ctx(tmp_path, harness, "build-graphcalc-m4")
    try:
        runs = [_owner_skip("P29"), _owner_skip("P30")]
        patch = milestone_gate(ctx)(_state(ctx, runs, ["P29", "P30"]))
        gate = patch["last_gate"]
        assert gate.verdict == "escalate_human" and gate.objective_met is False
        assert patch["phase"] == "gate"
        assert gate.reasoning.startswith("overridden: every feature was deferred by the owner")
    finally:
        ctx.close()
