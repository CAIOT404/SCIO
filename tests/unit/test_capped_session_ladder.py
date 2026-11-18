"""Issue #116: a worker session that runs out of turns without editing escalates by the
cheapest step first, and never counts as a stall on the way.

graphcalc P26 attempt 3: the T1 coder session spent its whole turn cap on a correct
diagnosis (``33 tool calls, stop=tool_cap``, ``files_touched: []``, 17 minutes), and SCIO
started a fresh debugger session that redid the reconnaissance. The stop only reached a
ledger line, so verify and triage could not tell "ran out of turns" from "chose to stop".

The ladder: (1) resume the same Claude Code session once, with tools, to write the fix;
(2) carry the findings (I-154, unchanged); (3) if it still edited nothing, the next session
on the step gets twice the turn cap, once per step, and that iteration counts no stall;
(4) after that, stalls count as before. Edits are judged from git, never from the worker.

The nodes run against a real git repository with a canned ``claude`` runner that edits the
tree like the binary would, and ``verify`` runs the real pytest and ruff.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.aci.toolbox import ToolBox
from scio.graph.nodes.implement import RESUME_PROMPT, debug, implement
from scio.graph.nodes.verify import verify
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.claude_code import ClaudeRunResult
from scio.harness.providers.fake import FakeProvider, FakeReply
from scio.harness.types import CompletionRequest, StopReason, ToolCall
from scio.loop import backends
from scio.loop.agent_loop import AgentLoop
from scio.loop.backends import RAISED_CAP_FACTOR, RESUME_TURN_CAP, ResumableWorker
from scio.loop.claude_code_worker import DEFAULT_MAX_TURNS, ClaudeCodeWorker
from scio.prompt.registry import PromptRegistry
from scio.settings import Settings
from scio.state.models import (
    FeatureItem,
    FeatureStatus,
    GitHubRefs,
    Plan,
    PlanStep,
    ProgressLedger,
    Role,
    SCIOState,
    TaskSpec,
    WorkerOutput,
)
from scio.state.signatures import advance
from scio.toolchain import python_toolchain

CODER_MODEL = ModelsConfig.load(Settings().models_path).role("coder").model
"""A real model id from models.yaml, so no id is written in code (check_docs)."""

FIXED = "def answer() -> int:\n    return 42\n"


# ---------- a canned ``claude`` that edits the tree like the binary ----------

Step = dict[str, Any] | list[dict[str, Any]] | Callable[[Path], list[dict[str, Any]]]


class ScriptedClaude:
    """Canned ``claude -p`` runs in call order. A callable step runs in the call's cwd first
    (it edits files), then returns its ``stream-json`` events. Records argv and stdin."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float = 0.0,
        env: Mapping[str, str] | None = None,
    ) -> ClaudeRunResult:
        self.calls.append((list(argv), stdin))
        nxt = self.steps.pop(0)
        if callable(nxt):
            nxt = nxt(Path(str(cwd)))
        if isinstance(nxt, list):
            return ClaudeRunResult(0, "\n".join(json.dumps(e) for e in nxt), "")
        return ClaudeRunResult(0, json.dumps(nxt), "")

    def argv(self, i: int) -> list[str]:
        return self.calls[i][0]


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _result(session: str, api_ms: int, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": session,
        "total_cost_usd": 0.01,
        "duration_api_ms": api_ms,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {CODER_MODEL: {"costUSD": 0.01}},
    }
    base.update(extra)
    return base


def _turn(session: str, *, capped: bool, api_ms: int = 1000) -> list[dict[str, Any]]:
    """One tool-using call's ``stream-json``: a Read, then the result line."""
    end = {"subtype": "error_max_turns", "is_error": True, "num_turns": 31} if capped else {}
    return [
        {"type": "system", "subtype": "init", "session_id": session},
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "x"}}
                ]
            },
        },
        _result(session, api_ms, **{"num_turns": 4, **end}),
    ]


def _editing(
    session: str, *, capped: bool, api_ms: int = 1000
) -> Callable[[Path], list[dict[str, Any]]]:
    """A tool-using call that writes the fix into the tree, as Claude Code's Edit would."""

    def run(cwd: Path) -> list[dict[str, Any]]:
        (cwd / "pkg" / "mod.py").write_text(FIXED, encoding="utf-8", newline="\n")
        return _turn(session, capped=capped, api_ms=api_ms)

    return run


def _summary(session: str, text: str = "diagnosed", api_ms: int = 1300) -> dict[str, Any]:
    out = {
        "step_id": "S1", "summary": text, "files_touched": [], "confidence": "medium",
        "needs_design_decision": False, "design_question": None, "next_action": "verify",
        "tests_added": ["tests/test_mod.py"], "sweep": None,
    }  # fmt: skip
    return _result(session, api_ms, structured_output=out)


# ---------- a real repository and a T1 state on step S1 ----------


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """``answer()`` returns 41 and its test wants 42; the toolchain's cache excludes are in
    ``info/exclude`` as ``prepare_repo`` writes them, so a test run is not an edit."""
    root = tmp_path / "repo"
    files = {
        "pkg/__init__.py": "",
        "pkg/mod.py": "def answer() -> int:\n    return 41\n",
        "tests/test_mod.py": (
            "from pkg.mod import answer\n\n\ndef test_answer() -> None:\n"
            "    assert answer() == 42\n"
        ),
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(python_toolchain().git_exclude_text(), encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path)


def _harness(settings: Settings, provider: FakeProvider | None = None) -> Harness:
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    if provider is not None:
        registry.register("fake", provider)
    return Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=10**8, usd_max=1000.0)),
    )


@pytest.fixture
def make_ctx(tmp_path: Path, repo: Path) -> Iterator[Callable[..., RunContext]]:
    made: list[RunContext] = []

    def make(backend: str = "claude_code", provider: FakeProvider | None = None) -> RunContext:
        settings = Settings(
            provider_override="fake", worker_backend=backend, sandbox_backend="local"
        )
        ctx = RunContext.create(
            thread_id="t-ladder",
            sandbox_root=repo,
            tier="T1",
            settings=settings,
            harness=_harness(settings, provider),
            sandbox=LocalSandbox(repo),
            work_dir=tmp_path / ".scio",
        )
        made.append(ctx)
        return ctx

    yield make
    for ctx in made:
        ctx.close()


def _state(repo: Path, ctx: RunContext, **progress: Any) -> SCIOState:
    return SCIOState(
        thread_id="t-ladder",
        phase="implement",
        task=TaskSpec(
            title="answer", description="answer() returns 42", acceptance=["42"], repo_url="r"
        ),
        budgets=ctx.budget,
        github=GitHubRefs(owner="o", repo="r", base_sha=_git(repo, "rev-parse", "HEAD")),
        feature_list=[FeatureItem(id="F1", description="42", test_filter="tests/test_mod.py")],
        plan=Plan(
            author=Role.PLANNER,
            steps=[
                PlanStep(
                    id="S1", description="fix", feature_ids=["F1"], files_expected=["pkg/mod.py"]
                )
            ],
            rationale="r",
        ),
        progress=ProgressLedger(current_step_id="S1", **progress),
    )


def _apply(state: SCIOState, patch: dict[str, Any]) -> SCIOState:
    """What LangGraph does with a node's patch: replace, but append ``task_ledger``."""
    update = {k: v for k, v in patch.items() if k != "task_ledger"}
    update["task_ledger"] = [*state.task_ledger, *patch.get("task_ledger", [])]
    return state.model_copy(update=update)


def _baseline(ctx: RunContext, state: SCIOState) -> SCIOState:
    """A first verify on the untouched tree: the fingerprint an unchanged tree repeats."""
    after = _apply(state, verify(ctx)(state))
    assert after.progress.last_failure_kind == "no_changes" and after.progress.stall_count == 0
    return after


# ---------- ladder step 1: resume the same session ----------


def test_a_capped_session_that_edited_nothing_is_resumed_once_with_its_tools(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = ScriptedClaude(
        [
            _turn("sess-a", capped=True),
            _summary("sess-a", "root cause found in pkg/mod.py"),
            _editing("sess-a", capped=False),
            _summary("sess-a", "fixed answer()"),
        ]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _apply(_state(repo, ctx), implement(ctx)(_state(repo, ctx)))

    assert len(claude.calls) == 4  # the session, its summary, ONE resume, its summary
    resume, stdin = claude.calls[2]
    assert _flag(resume, "--resume") == "sess-a" and stdin == RESUME_PROMPT
    assert _flag(resume, "--max-turns") == str(RESUME_TURN_CAP) == "10"  # bounded
    assert RESUME_TURN_CAP < DEFAULT_MAX_TURNS
    # every flag again: --permission-mode is not inherited on a -p resume (measured)
    assert "Edit" in _flag(resume, "--allowedTools")
    assert _flag(resume, "--permission-mode") == "acceptEdits"
    assert _flag(resume, "--permission-prompts") == "none"
    assert _flag(resume, "--output-format") == "stream-json" and "--verbose" in resume
    assert "--safe-mode" in resume and _flag(resume, "--model") == CODER_MODEL
    assert "--tools" not in resume  # the session's tools, as in the summary call (#185)
    assert "--system-prompt-file" not in resume  # the session keeps its system prompt
    summary = claude.argv(3)
    assert _flag(summary, "--resume") == "sess-a" and "--tools" not in summary
    assert "--allowedTools" not in summary  # tools listed, none allowed (I-250)
    p = state.progress
    assert (p.last_worker_stop, p.last_worker_edited, p.last_worker_resumed) == (
        "end_turn",
        True,
        True,
    )
    assert state.last_worker_output is not None
    assert state.last_worker_output.summary == "fixed answer()"
    assert any("ladder step 1 on S1" in e.text for e in state.task_ledger)


def test_a_resumed_session_that_edits_goes_to_verify_with_no_stall_counted(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the old code the capped session that edited nothing went straight to verify, which
    saw the same failure on the same tree and counted a stall."""
    claude = ScriptedClaude(
        [
            _turn("sess-a", capped=True),
            _summary("sess-a"),
            _editing("sess-a", capped=False),
            _summary("sess-a", "fixed"),
        ]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _baseline(ctx, _state(repo, ctx))
    state = _apply(state, implement(ctx)(state))
    state = _apply(state, verify(ctx)(state))
    assert state.progress.last_failure_kind == "none", state.task_ledger[-1].text
    assert state.progress.stall_count == 0 and state.exit_reason is None
    assert state.feature_list[0].status is FeatureStatus.PASSING


# ---------- ladder step 3: a raised cap, once per step, no stall ----------


def test_a_resume_that_still_edits_nothing_doubles_the_next_turn_cap_and_counts_no_stall(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = ScriptedClaude(
        [
            _turn("sess-a", capped=True),
            _summary("sess-a"),
            _turn("sess-a", capped=True),  # the resume reads on and edits nothing either
            _summary("sess-a", "still reading"),
            _editing("sess-b", capped=False),  # the debugger, with the raised cap
            _summary("sess-b", "fixed"),
        ]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _baseline(ctx, _state(repo, ctx))
    state = _apply(state, implement(ctx)(state))
    p = state.progress
    assert p.last_worker_resumed and p.last_worker_edited is False
    assert p.last_worker_stop == "tool_cap"
    assert p.raised_cap_pending and p.raised_cap_step == "v1:S1" and p.stall_exempt
    assert any("ladder step 3 on S1" in e.text for e in state.task_ledger)

    state = _apply(state, verify(ctx)(state))
    assert state.progress.last_failure_kind == "no_changes"
    assert state.progress.stall_count == 0  # the old code counted 1 here
    assert not state.progress.stall_exempt  # verify spent the exemption

    state = _apply(state, debug(ctx)(state))
    assert len(claude.calls) == 6  # the debugger's session edited: it was not resumed
    raised = str(DEFAULT_MAX_TURNS * RAISED_CAP_FACTOR)
    assert _flag(claude.argv(4), "--max-turns") == raised == "60"
    assert not state.progress.raised_cap_pending and state.progress.last_worker_edited
    assert _apply(state, verify(ctx)(state)).progress.last_failure_kind == "none"


def test_after_the_raised_cap_a_capped_session_that_edits_nothing_counts_a_stall_as_before(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = ScriptedClaude(
        [
            _turn("sess-b", capped=True),
            _summary("sess-b"),
            _turn("sess-b", capped=True),
            _summary("sess-b", "still reading"),
        ]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _baseline(ctx, _state(repo, ctx))
    granted = state.progress.model_copy(
        update={"raised_cap_step": "v1:S1", "raised_cap_pending": True}
    )
    state = _apply(state, debug(ctx)(state.model_copy(update={"progress": granted})))
    assert _flag(claude.argv(0), "--max-turns") == "60"  # the step's one raised session
    assert _flag(claude.argv(2), "--resume") == "sess-b"  # step 1 still applies to it
    p = state.progress
    assert not p.raised_cap_pending and not p.stall_exempt  # no second grant on the step
    assert any("stalls count as before" in e.text for e in state.task_ledger)
    state = _apply(state, verify(ctx)(state))
    assert state.progress.stall_count == 1  # step 4: counted as today


def test_a_replanned_step_that_reuses_an_id_gets_its_own_raised_cap(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant is keyed by plan version and step id: a replan's ``S1`` is a new step."""
    claude = ScriptedClaude(
        [_turn("s", capped=True), _summary("s"), _turn("s", capped=True), _summary("s")]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _state(repo, ctx, raised_cap_step="v1:S1")
    assert state.plan is not None
    replanned = state.model_copy(update={"plan": state.plan.model_copy(update={"version": 2})})
    p = _apply(replanned, implement(ctx)(replanned)).progress
    assert p.raised_cap_pending and p.raised_cap_step == "v2:S1" and p.stall_exempt


# ---------- never resumed ----------


@pytest.mark.parametrize(
    ("first", "stop", "edited"),
    [
        (_editing("sess-a", capped=True), "tool_cap", True),
        (_turn("sess-a", capped=False), "end_turn", False),
    ],
    ids=["capped-but-edited", "end-turn-without-edits"],
)
def test_a_session_that_edited_or_ended_on_end_turn_is_never_resumed(
    repo: Path,
    make_ctx: Callable[..., RunContext],
    monkeypatch: pytest.MonkeyPatch,
    first: Step,
    stop: str,
    edited: bool,
) -> None:
    claude = ScriptedClaude([first, _summary("sess-a")])
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    state = _apply(_state(repo, ctx), implement(ctx)(_state(repo, ctx)))
    assert len(claude.calls) == 2
    p = state.progress
    assert (p.last_worker_stop, p.last_worker_edited, p.last_worker_resumed) == (
        stop,
        edited,
        False,
    )
    assert not p.raised_cap_pending and not p.stall_exempt


def test_an_edit_on_top_of_an_earlier_sessions_uncommitted_work_counts_as_an_edit(
    repo: Path, make_ctx: Callable[..., RunContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``changed_since_head`` would say "edited" for any session after the first on a step;
    the ladder compares the tree before and after the session instead."""
    earlier = "def answer() -> int:\n    return 40\n"  # a previous session's work, uncommitted
    (repo / "pkg" / "mod.py").write_text(earlier, encoding="utf-8", newline="\n")
    claude = ScriptedClaude(
        [_turn("s", capped=True), _summary("s"), _turn("s", capped=True), _summary("s")]
    )
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", claude)
    ctx = make_ctx()
    p = _apply(_state(repo, ctx), implement(ctx)(_state(repo, ctx))).progress
    assert p.last_worker_edited is False and p.last_worker_resumed  # dirty tree, no new edit


# ---------- the aci backend: no session, so steps 3 and 4 only ----------


def _busy_provider(repo: Path) -> FakeProvider:
    """SCIO's own loop with a worker that only ever reads: it stops at its model-call cap."""
    calls = iter(range(10**6))
    summary = {
        "step_id": "S1", "summary": "reading", "files_touched": [], "confidence": "low",
        "needs_design_decision": False, "design_question": None, "next_action": "verify",
        "tests_added": [], "sweep": None,
    }  # fmt: skip

    def respond(req: CompletionRequest) -> FakeReply:
        if req.output_schema is not None:
            return FakeReply(text=json.dumps(summary))
        view = {"path": f"{repo.as_posix()}/pkg/mod.py", "start_line": 1}
        call = ToolCall(id=f"call-{next(calls)}", name="view_file", input=view)
        return FakeReply(text="", tool_calls=[call], stop_reason=StopReason.TOOL_USE)

    return FakeProvider(responder=respond)


def _tool_turns(provider: FakeProvider, role: str) -> int:
    return sum(1 for r in provider.requests if r.role == role and r.output_schema is None)


def test_the_aci_backend_never_resumes_and_a_capped_empty_session_takes_the_raised_cap(
    repo: Path, make_ctx: Callable[..., RunContext], tmp_path: Path
) -> None:
    provider = _busy_provider(repo)
    ctx = make_ctx("aci", provider)
    state = _baseline(ctx, _state(repo, ctx))
    state = _apply(state, implement(ctx)(state))
    p = state.progress
    assert _tool_turns(provider, "coder") == 12  # AgentLoop's model-call cap
    assert p.last_worker_stop == "model_cap" and p.last_worker_edited is False
    assert not p.last_worker_resumed  # no session to resume
    assert p.raised_cap_pending and p.stall_exempt
    state = _apply(state, verify(ctx)(state))
    assert state.progress.stall_count == 0
    state = _apply(state, debug(ctx)(state))
    assert _tool_turns(provider, "debugger") == 12 * RAISED_CAP_FACTOR
    assert not state.progress.raised_cap_pending
    assert _apply(state, verify(ctx)(state)).progress.stall_count == 1  # step 4 on aci
    toolbox = ToolBox(ctx.sandbox, ("view_file",), human_gate=False, role="coder")
    assert not isinstance(AgentLoop(ctx.harness, toolbox, PromptRegistry(), "coder"),
                          ResumableWorker)  # fmt: skip
    worker = ClaudeCodeWorker(
        ctx.harness, ctx.sandbox, PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("edit_file",),
    )  # fmt: skip
    assert isinstance(worker, ResumableWorker)


# ---------- I-210: each call's own latency ----------


def test_the_summary_and_resume_calls_record_their_own_latency_not_the_sessions(
    tmp_path: Path,
) -> None:
    """``duration_api_ms`` on a resumed call is the session's running total (measured on
    #116: 2,842 then 5,315 then 9,726 ms over three calls); graphcalc's ledger recorded the
    summary's 1,001,625 ms against the session's own 982,121."""
    settings = Settings(provider_override="fake", worker_backend="claude_code")
    harness = _harness(settings)
    claude = ScriptedClaude(
        [
            _turn("sess-a", capped=True, api_ms=1000),
            _summary("sess-a", api_ms=1300),
            _turn("sess-a", capped=False, api_ms=2500),
            _summary("sess-a", api_ms=2700),
            _turn("sess-b", capped=False, api_ms=800),  # another session starts from zero
            _summary("sess-b", api_ms=950),
        ]
    )
    worker = ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("edit_file",), runner=claude,
    )  # fmt: skip
    first = worker.run("task")
    assert first.session_id == "sess-a" and first.stop == "tool_cap"
    resumed = worker.resume_typed("sess-a", RESUME_PROMPT, WorkerOutput, max_turns=10)
    assert resumed.session_id == "sess-a" and resumed.stop == "end_turn"
    worker.run("another task")
    latencies = [e["latency_ms"] for e in harness.ledger.to_dict()["entries"]]
    assert latencies == [1000, 300, 1200, 200, 800, 150]


# ---------- the contract: old checkpoints load ----------

OLD_PROGRESS = {
    "current_step_id": "S1", "current_feature_id": None, "attempts_on_step": 2,
    "fix_attempts_on_error": 1, "last_error_signature": "abc123", "stall_count": 1,
    "iterations": 3, "tool_calls": 40, "last_fingerprint": "f00", "last_failure_kind": "tests",
    "gate_replans": 0,
}  # fmt: skip
"""``ProgressLedger`` as checkpoints before issue #116 stored it."""


def test_an_old_checkpoints_state_without_the_ladder_fields_loads(
    repo: Path, make_ctx: Callable[..., RunContext]
) -> None:
    dumped = _state(repo, make_ctx()).model_dump(mode="json")
    dumped["progress"] = dict(OLD_PROGRESS)
    state = SCIOState.model_validate(dumped)
    p = state.progress
    assert p.stall_count == 1 and p.iterations == 3
    assert p.last_worker_stop is None and p.last_worker_edited is None
    assert not (p.last_worker_resumed or p.raised_cap_pending or p.stall_exempt)
    assert p.raised_cap_step is None
    # and its next iteration counts a stall exactly as before the ladder
    nxt = advance(p.model_copy(update={"last_fingerprint": None}), state.feature_list,
                  error_text="AssertionError: x")  # fmt: skip
    again = advance(nxt, state.feature_list, error_text="AssertionError: x")
    assert again.stall_count == 1


def test_an_exempt_iteration_is_no_fix_attempt_so_advisor_cap_waits_for_the_raised_cap() -> None:
    """Step 4 is "halt only after that fails". The exempt iteration changed nothing, so it is
    not a fix attempt either. Counted, it brought the same error to ``repeat_threshold``, and
    with the step's advisor call spent, ``decide`` halted on ``advisor_cap`` before the
    raised-cap session ran (the orchestrator's review of the first draft)."""
    features = [FeatureItem(id="F1", description="42", test_filter="tests/test_mod.py")]
    err = "AssertionError: assert 41 == 42"
    p = advance(ProgressLedger(), features, error_text=err)
    p = advance(p, features, error_text=err)
    assert (p.fix_attempts_on_error, p.stall_count) == (2, 1)  # one short of repeat_threshold 3
    exempt = advance(p.model_copy(update={"stall_exempt": True}), features, error_text=err)
    assert exempt.fix_attempts_on_error == 2, "the exempt iteration is not a fix attempt"
    assert exempt.stall_count == 1, "nor a stall"
    assert exempt.iterations == 3 and not exempt.stall_exempt
    after = advance(exempt, features, error_text=err)  # the raised-cap session, still failing
    assert (after.fix_attempts_on_error, after.stall_count) == (3, 2)
    other = advance(p.model_copy(update={"stall_exempt": True}), features, error_text="E: new")
    assert other.fix_attempts_on_error == 1, "a new signature still starts at 1"
