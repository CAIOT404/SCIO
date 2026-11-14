"""A feature's plan is grounded in the repository (issue #121).

graphcalc P26 attempts 3 and 4: the T1 planner ran before its explorer and named
``graphcalc/evaluator.py``, which does not exist (the evaluator is ``graphcalc/calc.py``);
``localize`` appended the explorer's files afterwards and the guess stayed in the plan. Now
T1 explores first, the planner reads the summary, every named path is checked against the
tree, and a test filter that selects nothing is flagged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import scio.graph.nodes.planning as pl
from evals.fake_scripts import ScriptedResponder
from scio.context.projections import plan_message
from scio.graph.build import after_prepare_factory, build_graph
from scio.harness.providers.fake import FakeReply
from scio.prompt.registry import PromptRegistry
from scio.state.models import (
    Budget,
    EscalationTrigger,
    ExitReason,
    FeatureItem,
    FeatureStatus,
    GitHubRefs,
    Plan,
    PlanStep,
    Role,
    Routing,
    SCIOState,
    TaskSpec,
    Tier,
    Verdict,
)
from scio.toolchain import get_toolchain
from tests.graph.test_t1_t2_fake import GUESSED, REAL, GuessedFileResponder, _finish, _run

PY = get_toolchain("python")
ROOT = "/work/graphcalc"
# graphcalc's tree at P26 (abridged from ``git ls-files``)
TREE = [
    "README.md",
    "graphcalc/__init__.py",
    "graphcalc/__main__.py",
    "graphcalc/calc.py",
    "graphcalc/cli.py",
    "graphcalc/numfmt.py",
    "graphcalc/static/app.js",
    "tests/__init__.py",
    "tests/test_calc_evaluate.py",
    "tests/test_history.py",
]
TEST_TEXT = {
    "tests/test_calc_evaluate.py": "def test_evaluate_sums():\n    ...\n",
    "tests/test_history.py": "def test_history_keeps_order():\n    ...\n",
}


def _step(
    sid: str,
    files: list[str],
    *,
    desc: str = "fix",
    new: list[str] | None = None,
    feats: list[str] | None = None,
    done: bool = False,
) -> PlanStep:
    return PlanStep(
        id=sid,
        description=desc,
        feature_ids=feats or ["F1"],
        files_expected=files,
        files_new=new or [],
        done=done,
    )


def _plan(*steps: PlanStep) -> Plan:
    return Plan(author=Role.PLANNER, rationale="r", steps=list(steps))


def _ground(plan: Plan, localized: list[str]) -> tuple[Plan, list[str], set[str]]:
    return pl.ground_plan(
        plan, tree=TREE, localized=localized, is_test_path=PY.is_test_path, root=ROOT
    )


# ---------- the explorer before the planner on T1 ----------

MARKER = "the evaluator is pager/__init__.py:paginate (marker-121)"


class SummaryResponder(GuessedFileResponder):
    """Records every request; the localize explorer's findings carry a marker."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.requests: list[Any] = []

    def __call__(self, req: Any) -> FakeReply:
        self.requests.append(req)
        return super().__call__(req)

    def _structured(self, role: str, kind: str, text: str) -> FakeReply:
        if kind == "explorer":
            summary = {
                "question": "localize",
                "findings": [MARKER],
                "relevant_files": [f"{self.root}/{REAL}"],
                "open_questions": [],
            }
            return FakeReply(text=json.dumps(summary))
        return super()._structured(role, kind, text)


def test_t1_localize_runs_once_before_the_first_plan_and_the_planner_reads_its_summary(
    tmp_path: Path,
) -> None:
    """On the old graph the planner ran first: its request held no exploration summary."""
    responder = SummaryResponder()
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", responder)
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    roles = [r.role for r in responder.requests]
    assert roles.index("explorer") < roles.index("planner")
    planner = next(r for r in responder.requests if r.role == "planner")
    assert MARKER in planner.messages[0].text()
    explorer_summaries = [r for r in responder.requests if r.role == "explorer" and r.output_schema]
    assert len(explorer_summaries) == 1 and len(s.explorer_summaries) == 1
    texts = [e.text for e in s.task_ledger]
    assert texts.count("localize: repository context gathered") == 1
    first_plan = next(i for i, t in enumerate(texts) if t.startswith("plan v1:"))
    assert texts.index("localize: repository context gathered") < first_plan


def test_t1_a_guessed_path_is_replaced_by_the_localized_match_and_recorded(
    tmp_path: Path,
) -> None:
    """The planner names ``pager/core.py`` (no such file); the explorer localized
    ``pager/__init__.py``, the one file in that package. The old code kept both."""
    ctx, outcome = _run(
        tmp_path, "t002_fix_failing_test", "T1", GuessedFileResponder(), advisor_calls_max=0
    )
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS and s.plan is not None
    assert s.plan.steps[0].files_expected == [REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"plan grounding (v1): S1 {GUESSED} -> {REAL} (localized)" in texts
    assert not any("plan_drift" in t for t in texts)


class ExistingFileResponder(SummaryResponder):
    """The planner names a file that exists but is not the one the explorer found."""

    def _structured(self, role: str, kind: str, text: str) -> FakeReply:
        if kind == "planner":
            reply = json.loads(super()._structured(role, kind, text).text)
            for step in reply["steps"]:
                step["files_expected"] = ["README.md"]
            return FakeReply(text=json.dumps(reply))
        return super()._structured(role, kind, text)


def test_t1_localized_files_still_join_the_grounded_plan(tmp_path: Path) -> None:
    """I-151 holds after the reorder: the explorer's file joins a step that names others."""
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", ExistingFileResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS and s.plan is not None
    assert s.plan.steps[0].files_expected == ["README.md", REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"plan v1: files the explorer localized added to the plan: S1 += {REAL}" in texts
    assert not any(t.startswith("plan grounding") for t in texts)


def test_an_advisor_replan_is_grounded_and_localize_does_not_run_again(tmp_path: Path) -> None:
    """advise -> plan: plan v2 names the guess again; it is grounded the same way."""
    _ctx, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        GuessedFileResponder(unnamed=True, advisor_verdict="replan"),
    )
    s = outcome.state
    assert [(e.trigger, e.verdict) for e in s.escalations] == [
        (EscalationTrigger.PLAN_DRIFT, Verdict.REPLAN)
    ]
    assert s.plan is not None and s.plan.version == 2 and s.plan.author is Role.PLANNER
    assert s.plan.steps[0].files_expected == [REAL]
    assert len(s.explorer_summaries) == 1
    texts = [e.text for e in s.task_ledger]
    assert f"plan grounding (v2): S1 {GUESSED} -> {REAL} (localized)" in texts
    assert texts.count("localize: repository context gathered") == 1


def test_the_advisors_revised_steps_are_grounded_too(tmp_path: Path) -> None:
    _ctx, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        GuessedFileResponder(unnamed=True, advisor_verdict="replan", revised_steps=True),
    )
    s = outcome.state
    assert s.plan is not None and s.plan.version == 2 and s.plan.author is Role.ADVISOR
    assert s.plan.steps[0].files_expected == [REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"plan grounding (v2): S1 {GUESSED} -> {REAL} (localized)" in texts


def test_a_test_filter_that_matches_nothing_is_flagged_not_dropped(tmp_path: Path) -> None:
    """t004's filters ``circle`` and ``rect`` select no test yet, and the scripted plan
    names no test file; the coder writes them, and the run passes."""
    ctx, outcome = _run(tmp_path, "t004_two_features", "T1", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert {f.id: f.test_filter for f in s.feature_list} == {"F1": "circle", "F2": "rect"}
    texts = [e.text for e in s.task_ledger]
    assert (
        "plan grounding (v1): F1's test filter 'circle' (S1) matches no test in the tree, and "
        "S1 creates no test file; kept (the coder may write it)"
    ) in texts
    assert any(t.startswith("plan grounding (v1): F2's test filter 'rect' (S2)") for t in texts)


# ---------- routing: T0 and T2 unchanged ----------


def _edges(tier: str) -> set[tuple[str, str]]:
    graph = build_graph(None, tier).compile().get_graph()  # type: ignore[arg-type]
    return {(e.source, e.target) for e in graph.edges}


def _routed(tier: Tier, *, plan: Plan | None = None) -> SCIOState:
    return SCIOState(
        thread_id="t",
        task=TaskSpec(title="t", description="d", acceptance=[], repo_url="x"),
        budgets=Budget(tokens_max=1, usd_max=1.0, advisor_calls_max=0),
        github=GitHubRefs(owner="o", repo="r"),
        routing=Routing(
            tier=tier,
            pattern="plan_execute_gate",
            effort_tier="medium",
            rationale="r",
            classifier_model="m",
            confidence=1.0,
        ),
        plan=plan,
    )


def test_t1_explores_before_it_plans_and_t0_t2_routing_is_unchanged() -> None:
    after_prepare = after_prepare_factory({"T0", "T1", "T2"})
    assert after_prepare(_routed(Tier.T0)) == "t0_loop"
    assert after_prepare(_routed(Tier.T1)) == "localize"
    assert after_prepare(_routed(Tier.T2)) == "scope"
    t1 = _edges("T1")
    assert {("prepare_repo", "localize"), ("localize", "plan"), ("post_plan", "implement")} <= t1
    assert ("post_plan", "localize") not in t1
    assert {("advise", "plan"), ("gate", "plan")} <= t1  # replans return to plan, not localize
    t2 = _edges("T2")
    assert {("prepare_repo", "scope"), ("explore", "plan"), ("post_plan", "implement")} <= t2
    assert not any("localize" in e for e in t2)
    t0 = _edges("T0")
    assert ("prepare_repo", "t0_loop") in t0
    assert not any(n in {"plan", "localize"} for e in t0 for n in e)


def test_an_old_checkpoint_paused_before_localize_goes_on_to_implement() -> None:
    """Before #121, localize ran after post_plan; such a thread already has its plan."""
    assert pl.after_localize(_routed(Tier.T1)) == "plan"
    assert pl.after_localize(_routed(Tier.T1, plan=_plan(_step("S1", ["a.py"])))) == "implement"
    halted = _routed(Tier.T1).model_copy(update={"exit_reason": ExitReason.BUDGET})
    assert pl.after_localize(halted) == "halt"


def test_old_plans_and_checkpoints_load_without_files_new() -> None:
    old_step = {"id": "S1", "description": "d", "feature_ids": ["F1"], "files_expected": ["a.py"]}
    assert PlanStep.model_validate(old_step).files_new == []
    state = _routed(Tier.T1, plan=_plan(_step("S1", ["a.py"]))).model_dump(mode="json")
    for step in state["plan"]["steps"]:
        del step["files_new"]  # a checkpoint written before the field existed
    loaded = SCIOState.model_validate(state)
    assert loaded.plan is not None and loaded.plan.steps[0].files_new == []


# ---------- ground_plan ----------


def test_p26_guessed_evaluator_is_replaced_by_the_localized_file_in_its_package() -> None:
    """Both P26 paths were guesses; attempt 4 then created ``state.py``. ``calc.py`` stands
    in for the first guess only, so ``state.py`` stays as a probable new file."""
    plan, notes, named = _ground(
        _plan(_step("S1", ["graphcalc/evaluator.py", "graphcalc/state.py"])),
        localized=["graphcalc/calc.py", "tests/test_calc_evaluate.py"],
    )
    assert plan.steps[0].files_expected == ["graphcalc/calc.py", "graphcalc/state.py"]
    assert notes == [
        "S1 graphcalc/evaluator.py -> graphcalc/calc.py (localized)",
        "S1 kept graphcalc/state.py (probable new file: its directory exists)",
    ]
    assert named == {"S1"} and plan.version == 1


def test_a_guess_in_an_existing_directory_is_kept_as_a_probable_new_file() -> None:
    """Two candidates in its package: no obvious match. Dropping it would make the coder's
    new file drift; an expected file nobody touches costs nothing."""
    plan, notes, _ = _ground(
        _plan(_step("S1", ["graphcalc/cli.py", "graphcalc/evaluator.py", "setup_extra.py"])),
        localized=["graphcalc/calc.py", "graphcalc/numfmt.py"],
    )
    assert plan.steps[0].files_expected == [
        "graphcalc/cli.py",
        "graphcalc/evaluator.py",
        "setup_extra.py",  # the repository root exists too
    ]
    assert notes == [
        "S1 kept graphcalc/evaluator.py (probable new file: its directory exists)",
        "S1 kept setup_extra.py (probable new file: its directory exists)",
    ]


def test_a_guess_whose_directory_does_not_exist_is_dropped_and_recorded() -> None:
    plan, notes, _ = _ground(
        _plan(_step("S1", ["graphcalc/cli.py", "graphcalc/engine/evaluator.py"])),
        localized=["graphcalc/calc.py", "graphcalc/numfmt.py"],
    )
    assert plan.steps[0].files_expected == ["graphcalc/cli.py"]
    assert notes == [
        "S1 graphcalc/engine/evaluator.py dropped (not in the tree, not declared new, and "
        "its directory does not exist)"
    ]


def test_the_fake_http_products_new_store_is_kept() -> None:
    """The scripted planner declares no ``files_new``; ``todo_api/store.py`` and the new
    test file live in directories that exist, so the Fake product runs keep them."""
    from evals.product_scripts_http import HTTP_PRODUCT_SCRIPTS

    script = next(s for s in HTTP_PRODUCT_SCRIPTS if s.key == "create and list todos")
    step = script.steps[0]
    tree = ["todo_api/__init__.py", "todo_api/cli.py", "tests/test_health.py"]
    plan, notes, _ = pl.ground_plan(
        _plan(_step(step.id, list(step.files_expected), desc=step.description)),
        tree=tree,
        localized=[],
        is_test_path=PY.is_test_path,
    )
    assert plan.steps[0].files_expected == step.files_expected
    assert "todo_api/store.py" in step.files_expected
    assert all("dropped" not in n for n in notes)


def test_same_basename_and_short_spellings_resolve_to_the_existing_file() -> None:
    plan, notes, _ = _ground(
        _plan(_step("S1", ["src/graphcalc/calc.py", "cli.py"])), localized=["graphcalc/calc.py"]
    )
    assert plan.steps[0].files_expected == ["graphcalc/calc.py", "graphcalc/cli.py"]
    assert notes == [
        "S1 src/graphcalc/calc.py -> graphcalc/calc.py (localized)",
        "S1 cli.py -> graphcalc/cli.py (tree)",
    ]


def test_a_declared_new_file_is_kept_and_joins_files_expected() -> None:
    plan, notes, _ = _ground(
        _plan(
            _step("S1", ["graphcalc/calc.py", "graphcalc/units.py"], new=["graphcalc/units.py"]),
            _step("S2", ["graphcalc/cli.py"], new=["graphcalc/plot.py"]),
        ),
        localized=["graphcalc/calc.py"],
    )
    assert plan.steps[0].files_expected == ["graphcalc/calc.py", "graphcalc/units.py"]
    assert plan.steps[1].files_expected == ["graphcalc/cli.py", "graphcalc/plot.py"]
    assert notes == []


def test_a_new_test_path_is_kept_when_the_step_says_it_writes_it() -> None:
    plan, notes, _ = _ground(
        _plan(
            _step("S1", ["graphcalc/calc.py", "tests/test_units.py"], desc="add units with tests"),
            _step("S2", ["graphcalc/calc.py", "tests/unit/test_other.py"], desc="fix rounding"),
        ),
        localized=[],
    )
    assert plan.steps[0].files_expected == ["graphcalc/calc.py", "tests/test_units.py"]
    assert plan.steps[1].files_expected == ["graphcalc/calc.py"]
    assert notes == [
        "S2 tests/unit/test_other.py dropped (not in the tree, not declared new, and its "
        "directory does not exist)"
    ]


def test_existing_files_directories_absolute_paths_and_done_steps_stay() -> None:
    plan, notes, named = _ground(
        _plan(
            _step("S1", ["graphcalc/static/", f"{ROOT}/graphcalc/cli.py"]),
            _step("S2", ["gone.py"], done=True),
            _step("S3", []),
        ),
        localized=[],
    )
    assert plan.steps[0].files_expected == ["graphcalc/static/", f"{ROOT}/graphcalc/cli.py"]
    assert plan.steps[1].files_expected == ["gone.py"] and plan.steps[2].files_expected == []
    assert notes == [] and named == {"S1"}


def test_a_step_whose_every_guess_dropped_still_gets_the_localized_files() -> None:
    """An empty files_expected turns drift off; a step that named files keeps drift on."""
    plan, notes, named = _ground(
        _plan(_step("S1", ["graphcalc/engine/evaluator.py"])), localized=[]
    )
    assert plan.steps[0].files_expected == []
    assert notes[-1] == "S1 keeps none of the files it named"
    merged, added = pl.add_localized_files(plan, ["graphcalc/calc.py"], root=ROOT, named=named)
    assert merged.steps[0].files_expected == ["graphcalc/calc.py"]
    assert added == {"S1": ["graphcalc/calc.py"]}


# ---------- test filters ----------


def _grep(terms: list[str]) -> set[str]:
    return {p for p, text in TEST_TEXT.items() if any(t.lower() in text.lower() for t in terms)}


def _flags(plan: Plan, features: list[FeatureItem]) -> list[str]:
    return pl.unmatched_test_filters(
        plan,
        features,
        tree=TREE,
        is_test_path=PY.is_test_path,
        files_containing=_grep,
        root=ROOT,
    )


def test_test_filters_that_select_nothing_are_flagged() -> None:
    features = [
        FeatureItem(id="F1", description="d", test_filter="evaluate_sums"),  # in a test's text
        FeatureItem(id="F2", description="d", test_filter="history and not slow"),  # a path
        FeatureItem(id="F3", description="d", test_filter="unicode_names"),  # nowhere
        FeatureItem(id="F4", description="d", test_filter="tests/test_missing.py"),  # no file
        FeatureItem(id="F5", description="d", test_filter="tests/test_units.py::test_x"),
        FeatureItem(id="F6", description="d", test_filter="nothing", status=FeatureStatus.PASSING),
    ]
    plan = _plan(
        _step("S1", ["graphcalc/calc.py"], feats=["F1", "F2", "F3", "F4", "F6"]),
        _step("S2", ["graphcalc/calc.py", "tests/test_units.py"], feats=["F5"]),
    )
    assert _flags(plan, features) == [
        "F3's test filter 'unicode_names' (S1) matches no test in the tree, and S1 creates no "
        "test file; kept (the coder may write it)",
        "F4's test filter 'tests/test_missing.py' (S1) matches no test in the tree, and S1 "
        "creates no test file; kept (the coder may write it)",
    ]
    assert features[2].test_filter == "unicode_names"  # flagged, never dropped


def test_a_step_that_creates_a_test_file_is_not_flagged() -> None:
    features = [FeatureItem(id="F1", description="d", test_filter="unicode_names")]
    plan = _plan(_step("S1", ["graphcalc/calc.py", "tests/test_names.py"], desc="add names"))
    assert _flags(plan, features) == []


def test_an_unreadable_tree_is_no_flag() -> None:
    features = [FeatureItem(id="F1", description="d", test_filter="unicode_names")]
    plan = _plan(_step("S1", ["graphcalc/calc.py"]))
    assert (
        pl.unmatched_test_filters(
            plan,
            features,
            tree=TREE,
            is_test_path=PY.is_test_path,
            files_containing=lambda _terms: None,
        )
        == []
    )


# ---------- the planner's input and prompt ----------


def test_the_planner_prompt_asks_for_real_or_declared_files() -> None:
    prompt = PromptRegistry().get("planner")
    assert prompt.version == 3
    assert "files_new" in prompt.text and "Never guess a module's path" in prompt.text


def test_a_replan_shows_the_previous_steps_new_files() -> None:
    task = TaskSpec(title="t", description="d", acceptance=["a"], repo_url="x")
    previous = _plan(_step("S1", ["graphcalc/units.py"], new=["graphcalc/units.py"]))
    msg = plan_message(task, [], summaries=["s"], previous=previous, revision_reason="r")
    assert "files graphcalc/units.py; new graphcalc/units.py)" in msg
