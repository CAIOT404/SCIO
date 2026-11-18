"""Issue #169, part B (I-248): a feature whose own tests were skipped is not verified.

graphcalc's P33 wrote its browser tests behind ``pytest.importorskip("playwright.sync_api")``.
Where Playwright was absent they skipped, the run read ``all tests passed``, and the feature,
the integration and the Opus gate all passed with its rendered criteria never checked. Part A
(I-242) names such skips: ``run_tests`` adds a ``skipped (not verified): path: reason`` line,
integration a ``[test skips]`` line. Part B acts on them. A feature's own selector run that
skipped a test fails its verify, with the skip and its reason in its output, and the
milestone gate reads the ``[test skips]`` line as unmet. A skip elsewhere in the suite, and an
acceptance test's skip (it runs for real against the started app), fail neither.

Verify runs the real pytest and ruff on a real repository, as a feature run does.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from scio.aci.tools import SKIPS_NOTE
from scio.graph.nodes.verify import UNVERIFIED_SKIP_HINT, verify
from scio.graph.run_context import RunContext
from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest
from scio.product.build import GATE_REASONING_MAX, TEST_SKIPS_CHECK, after_gate, milestone_gate
from scio.state.models import (
    FeatureItem,
    FeatureRun,
    FeatureStatus,
    GateOutput,
    GitHubRefs,
    MilestoneFeatureOutput,
    MilestonePlan,
    Plan,
    PlanStep,
    ProductBuildState,
    ProductFeature,
    ProductSpec,
    ProgressLedger,
    Role,
    SCIOState,
    TaskSpec,
    Tier,
    WorkerOutput,
)
from scio.toolchain.base import Skip, skips_text
from tests.graph.test_product_build_fake import PRODUCT, _ctx, _harness, _settings

PAGE_TEST = (
    "import pytest\n\n\n"
    "def test_the_page_is_served():\n    assert True\n\n\n"
    "def test_the_page_draws():\n"
    '    pytest.importorskip("no_such_browser_module")\n'
)
"""One test that runs and one that skips where its browser module is absent (graphcalc P33)."""
SERVED_TEST = "def test_the_index_is_served():\n    assert True\n"
APP_SKIP = "APP_URL not set: scio build starts the app for these tests"
BLACK_BOX_TEST = (
    "import pytest\n\n\n"
    f'@pytest.mark.skip(reason="{APP_SKIP}")\n'
    "def test_health_black_box():\n    assert True\n"
)
HEALTH_TEST = "def test_health_in_process():\n    assert True\n"

# ---------- verify on a real repository ----------


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _gate_reply(
    verdict: str = "pass", reasoning: str = "every criterion is met"
) -> Callable[[CompletionRequest], FakeReply]:
    """The milestone gate's answer, the only model call these tests make."""

    def respond(_req: CompletionRequest) -> FakeReply:
        met = verdict == "pass"
        body = {
            "verdict": verdict,
            "objective_met": met,
            "unmet_acceptance": [] if met else ["(scripted) criteria judged unmet"],
            "reasoning": reasoning,
        }
        return FakeReply(text=json.dumps(body))

    return respond


def _product(
    tmp_path: Path, files: dict[str, str], *, verdict: str = "pass", reasoning: str = "met"
) -> tuple[RunContext, str]:
    """A product repository: a base commit, then ``files`` as the feature's change. Returns
    a context on it and the base commit."""
    root = tmp_path / "product"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# product\n", encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    harness = _harness(_settings(tmp_path), _gate_reply(verdict, reasoning))
    return _ctx(tmp_path, harness, "build-graphcalc-m7"), _git(root, "rev-parse", "HEAD")


def _task(
    ctx: RunContext, base: str, test_filter: str, worker: WorkerOutput | None = None
) -> SCIOState:
    """A feature run on step S1 (T1), with F1 proved by ``test_filter``."""
    return SCIOState(
        thread_id=ctx.thread_id,
        phase="implement",
        task=TaskSpec(
            title="P33", description="the page wraps", acceptance=["it wraps"], repo_url="r"
        ),
        budgets=ctx.budget,
        github=GitHubRefs(owner="o", repo="r", base_sha=base),
        feature_list=[FeatureItem(id="F1", description="it wraps", test_filter=test_filter)],
        plan=Plan(
            author=Role.PLANNER,
            steps=[PlanStep(id="S1", description="build", feature_ids=["F1"], files_expected=[])],
            rationale="r",
        ),
        progress=ProgressLedger(current_step_id="S1"),
        last_worker_output=worker,
    )


def _verify(ctx: RunContext, state: SCIOState) -> tuple[FeatureItem, str, str]:
    """F1 after one verify, the failure kind, and the stored test output."""
    patch = verify(ctx)(state)
    (feature,) = patch["feature_list"]
    text = ctx.artifacts.get(patch["artifacts"]["verify:last"])
    return feature, patch["progress"].last_failure_kind, text


def _own_and_suite(text: str) -> tuple[str, str]:
    """The feature's own selector runs, and the whole-suite run."""
    own, suite = text.split("[run_tests all]", 1)
    return own, suite


# ---------- the milestone gate ----------

PAGE_SKIP = Skip(
    1, "tests/test_p33_wrapping.py", "could not import 'playwright.sync_api': No module named 'x'"
)
SKIP_LINE = f"[{TEST_SKIPS_CHECK}] 1 outside tests/acceptance: {skips_text([PAGE_SKIP])}"
"""The line ``integration_checks`` writes for graphcalc's skipped browser module."""
SUITE = [
    "[lint] clean",
    "[tests] passed",
    "........s.s",
    "=========================== short test summary info ============================",
    f"SKIPPED [1] tests/acceptance/test_acceptance.py:12: {APP_SKIP}",
]
GREEN = "\n".join([*SUITE, "365 passed, 1 skipped in 9.80s", "[acceptance] passed: 4 passed"])
"""Integration on a milestone whose only skip is an acceptance test's: no ``[test skips]``."""
SKIPPED = "\n".join(
    [
        *SUITE,
        "SKIPPED [1] tests/test_p33_wrapping.py:3: could not import 'playwright.sync_api'",
        "365 passed, 2 skipped in 9.80s",
        SKIP_LINE,
        "[acceptance] passed: 4 passed",
    ]
)


def _gate_state(ctx: RunContext, integration: str) -> ProductBuildState:
    """Milestone 7 with P33 passed and green checks; ``integration`` is the report."""
    plan = MilestonePlan(
        number=7,
        title="wrapping",
        branch="milestone/7",
        features=[MilestoneFeatureOutput(id="P33")],
    )
    return ProductBuildState(
        thread_id=ctx.thread_id,
        product_dir=str(ctx.sandbox.root),
        spec=ProductSpec(
            name=PRODUCT,
            title="graphcalc",
            vision="v",
            users=["u"],
            features=[
                ProductFeature(
                    id="P33", description="results wrap", acceptance=["scrollWidth <= innerWidth"]
                )
            ],
            quality_bar=["q"],
        ),
        milestone=plan,
        feature_runs=[
            FeatureRun(
                feature_id="P33",
                thread_id="build-graphcalc-m7-p33-a1",
                tier=Tier.T1,
                status="passed",
                milestone=plan.id,
                commit_sha="a" * 40,
            )
        ],
        integration=integration,
        integration_ok=True,
        budgets=ctx.budget,
    )


# ---------- the invariants ----------


def test_a_feature_whose_own_test_skipped_fails_verify(tmp_path: Path) -> None:
    """The owner: "A feature whose own tests were skipped is not verified." Its selector run
    reads ``all tests passed`` plus a ``skipped (not verified)`` line: the feature is FAILING,
    as a test failure, so the coder's next turn makes the test run."""
    ctx, base = _product(tmp_path, {"tests/test_page.py": PAGE_TEST})
    try:
        feature, kind, text = _verify(ctx, _task(ctx, base, "tests/test_page.py"))
        own, _suite = _own_and_suite(text)
        assert own.splitlines()[1].startswith("all tests passed: 1 passed, 1 skipped in "), own
        assert f"\n{SKIPS_NOTE}: tests/test_page.py: " in own
        assert feature.status is FeatureStatus.FAILING
        assert kind == "tests"
    finally:
        ctx.close()


def test_the_failed_verify_names_the_skip_and_its_reason(tmp_path: Path) -> None:
    """The owner: "The skip and its reason are in the feature's result." The stored test
    output, the feature's evidence and what the coder reads next, names the skipped file, the
    reason the runner gave, and what to do about it."""
    ctx, base = _product(tmp_path, {"tests/test_page.py": PAGE_TEST})
    try:
        feature, _kind, text = _verify(ctx, _task(ctx, base, "tests/test_page.py"))
        assert feature.evidence is not None and ctx.artifacts.get(feature.evidence) == text
        own, _suite = _own_and_suite(text)
        skip = next(ln for ln in own.splitlines() if ln.startswith(f"{SKIPS_NOTE}: "))
        assert skip.startswith(f"{SKIPS_NOTE}: tests/test_page.py: could not import "), skip
        assert "no_such_browser_module" in skip
        assert skip.endswith(UNVERIFIED_SKIP_HINT)
    finally:
        ctx.close()


def test_the_milestone_gate_does_not_pass_with_skipped_tests(tmp_path: Path) -> None:
    """The owner: "and the gate reads it as unmet." The integration report has a
    ``[test skips]`` line and Opus says ``pass``: the verdict is ``escalate_human``, the skip
    is unmet, and the build stops at ``gate_decision``, where a human may still proceed."""
    ctx, _base = _product(tmp_path, {}, verdict="pass")
    try:
        state = _gate_state(ctx, SKIPPED)
        patch = milestone_gate(ctx)(state)
        gate = patch["last_gate"]
        assert gate.verdict == "escalate_human" and gate.objective_met is False
        assert patch["phase"] == "gate"
        assert after_gate(state.model_copy(update={"last_gate": gate})) == "gate_decision"
        why = gate.reasoning.split(". ", 1)[0]
        assert why.startswith("overridden: tests skipped outside tests/acceptance: "), why
        assert "tests/test_p33_wrapping.py" in why and "playwright.sync_api" in why
        assert SKIP_LINE in gate.unmet_acceptance
    finally:
        ctx.close()


def test_a_skip_outside_the_features_own_tests_does_not_fail_its_verify(tmp_path: Path) -> None:
    """Non-regression: the whole-suite run is the regression check, and its skips are the
    integration's and the gate's to report. A skip in another module fails no feature whose
    own tests ran. The same module as the feature's own test does fail it."""
    files = {"tests/test_index.py": SERVED_TEST, "tests/test_page.py": PAGE_TEST}
    ctx, base = _product(tmp_path, files)
    try:
        feature, kind, text = _verify(ctx, _task(ctx, base, "tests/test_index.py"))
        own, suite = _own_and_suite(text)
        assert f"\n{SKIPS_NOTE}: tests/test_page.py: " in suite  # the suite did skip
        assert SKIPS_NOTE not in own and UNVERIFIED_SKIP_HINT not in text
        assert feature.status is FeatureStatus.PASSING and kind == "none"
        # the contrast: the skipping module as the feature's own test
        feature, kind, _text = _verify(ctx, _task(ctx, base, "tests/test_page.py"))
        assert feature.status is FeatureStatus.FAILING and kind == "tests"
    finally:
        ctx.close()


def test_an_acceptance_skip_fails_neither_verify_nor_the_gate(tmp_path: Path) -> None:
    """Non-regression: an acceptance test skips outside integrate (no started app) and runs
    for real against it, so its skip produces no skip line. Verify passes, and a gate with no
    ``[test skips]`` line passes. The same test outside the acceptance directory fails."""
    files = {
        "tests/acceptance/test_acceptance.py": BLACK_BOX_TEST,
        "tests/test_health.py": HEALTH_TEST,
    }
    ctx, base = _product(tmp_path, files, verdict="pass")
    try:
        feature, kind, text = _verify(ctx, _task(ctx, base, "health"))
        own, _suite = _own_and_suite(text)
        assert "all tests passed: 1 passed, 1 skipped in " in own  # the acceptance test skipped
        assert SKIPS_NOTE not in text
        assert feature.status is FeatureStatus.PASSING and kind == "none"
        patch = milestone_gate(ctx)(_gate_state(ctx, GREEN))
        gate = patch["last_gate"]
        assert gate.verdict == "pass" and patch["phase"] == "pr", gate.reasoning
        assert gate.unmet_acceptance == [] and not gate.reasoning.startswith("overridden")
        # the contrast: the same skipped test outside the acceptance directory
        root = tmp_path / "product"
        (root / "tests" / "test_black_box.py").write_text(
            BLACK_BOX_TEST, encoding="utf-8", newline="\n"
        )
        (root / "tests" / "acceptance" / "test_acceptance.py").unlink()
        feature, kind, text = _verify(ctx, _task(ctx, base, "health"))
        assert f"\n{SKIPS_NOTE}: tests/test_black_box.py: {APP_SKIP}" in text
        assert feature.status is FeatureStatus.FAILING and kind == "tests"
    finally:
        ctx.close()


# ---------- the edges ----------


def test_a_plan_filter_that_skipped_falls_back_to_the_workers_tests(tmp_path: Path) -> None:
    """The plan names tests before they exist (I-037): when its filter fails only on an
    unproven run (matched nothing, skipped only, or now a skipped test), the tests the worker
    reports are tried, and adopted when they pass and run."""
    files = {"tests/test_index.py": SERVED_TEST, "tests/test_page.py": PAGE_TEST}
    ctx, base = _product(tmp_path, files)
    worker = WorkerOutput(
        step_id="S1",
        summary="served the index",
        files_touched=["tests/test_index.py"],
        confidence="high",
        next_action="verify",
        tests_added=["tests/test_index.py"],
    )
    try:
        state = _task(ctx, base, "tests/test_page.py", worker)
        feature, kind, text = _verify(ctx, state)
        assert "trying worker-reported tests" in text
        assert feature.test_filter == "tests/test_index.py"
        assert feature.status is FeatureStatus.PASSING and kind == "none"
    finally:
        ctx.close()


def test_the_skip_is_unmet_whatever_the_verdict_and_the_reason_still_fits(
    tmp_path: Path,
) -> None:
    """A gate that already wants a human lists the skip as unmet too. An overridden
    reason goes in front of the model's own, cut to ``GateOutput.reasoning``'s limit, so the
    stored gate still validates when a checkpoint is read back."""
    long = "r" * GATE_REASONING_MAX
    ctx, _base = _product(tmp_path, {}, verdict="pass", reasoning=long)
    try:
        gate = milestone_gate(ctx)(_gate_state(ctx, SKIPPED))["last_gate"]
        assert len(gate.reasoning) == GATE_REASONING_MAX
        assert gate.reasoning.startswith("overridden: tests skipped outside tests/acceptance: ")
        assert GateOutput.model_validate(gate.model_dump()) == gate
    finally:
        ctx.close()
    ctx, _base = _product(tmp_path / "again", {}, verdict="escalate_human")
    try:
        gate = milestone_gate(ctx)(_gate_state(ctx, SKIPPED))["last_gate"]
        assert gate.verdict == "escalate_human" and not gate.reasoning.startswith("overridden")
        assert gate.unmet_acceptance == ["(scripted) criteria judged unmet", SKIP_LINE]
    finally:
        ctx.close()
