"""``scripts/dev/claude_probe.py`` and its helpers (issue #122, W-30 and W-31).

The probe runs ``claude -p`` with the flags ``ClaudeCodeWorker`` passes, built by the same
functions (``worker_argv``, ``resume_argv``), so a probe of Claude Code's behaviour checks
what the worker really sends. No test runs a live ``claude``: the backend tests' fake runner
stands in for it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.graph.nodes.implement import WRITER_TOOLS
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.loop.claude_code_worker import ClaudeCodeWorker
from scio.prompt.registry import PromptRegistry
from scio.settings import REPO_ROOT, Settings
from scio.state.models import WorkerOutput
from scio.toolchain import python_toolchain
from tests.unit.test_claude_code_backend import STREAM, FakeClaude, _result

SUMMARY = {
    "step_id": "S1", "summary": "done", "files_touched": [], "confidence": "high",
    "needs_design_decision": False, "design_question": None, "next_action": "verify",
    "tests_added": [], "sweep": None,
}  # fmt: skip


def _load(name: str) -> ModuleType:
    path = REPO_ROOT / "scripts" / "dev" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"dev_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _masked(argv: list[str]) -> list[str]:
    """``argv`` with the system prompt file's path masked: the worker writes it to a
    temporary directory, the probe to its output directory."""
    out = list(argv)
    if "--system-prompt-file" in out:
        out[out.index("--system-prompt-file") + 1] = "<system prompt file>"
    return out


def _worker(tmp_path: Path, fake: FakeClaude, *, binding: bool) -> ClaudeCodeWorker:
    settings = Settings(provider_override="fake", worker_backend="claude_code")
    config = ModelsConfig.load(settings.models_path)
    budget = BudgetGuard(BudgetLimits(tokens_max=1_000_000, usd_max=3.0), binding=binding)
    harness = Harness(config, ProviderRegistry(config, settings), settings=settings, budget=budget)
    return ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path / "repo"), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=WRITER_TOOLS, max_turns=10, runner=fake,
    )  # fmt: skip


def test_the_probe_harness_uses_scios_exact_worker_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-31: a probe passes what ``ClaudeCodeWorker.run_typed`` passes for the same inputs,
    every flag in the same order, with the same system prompt text and the same environment
    (no ``CLAUDECODE``, no ``ANTHROPIC_API_KEY``); and what ``resume_typed`` passes for a
    ``--resume``. On the old code ``worker_argv`` did not exist, and the probe could only copy
    the flags."""
    probe = _load("claude_probe")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-placeholder")
    coder_model = ModelsConfig.load(Settings().models_path).role("coder").model
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Task: do it", encoding="utf-8", newline="\n")

    # a new session, the budget binding: --max-budget-usd is passed
    worker_fake = FakeClaude([STREAM, _result(session_id="sess-w", structured_output=SUMMARY)])
    _worker(tmp_path, worker_fake, binding=True).run("# Task: do it")
    probe_fake = FakeClaude([STREAM])
    out = tmp_path / "out"
    code = probe.main(
        ["--prompt-file", str(prompt), "--model", coder_model, "--tools", "coder",
         "--max-turns", "10", "--budget-usd", "3", "--cwd", str(tmp_path / "cwd"),
         "--out", str(out)],
        runner=probe_fake,
    )  # fmt: skip
    assert code == 0
    worker_argv, probe_argv = worker_fake.calls[0][0], probe_fake.calls[0][0]
    assert "--max-budget-usd" in worker_argv and "--name" in worker_argv
    assert _masked(probe_argv) == _masked(worker_argv)
    assert probe_fake.systems == worker_fake.systems[:1]  # the role's prompt + BACKEND_NOTE
    assert probe_fake.calls[0][2] == "# Task: do it"  # the prompt file goes on stdin
    env = probe_fake.envs[0]
    assert env is not None and env == worker_fake.envs[0]
    assert "CLAUDECODE" not in env and "ANTHROPIC_API_KEY" not in env

    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert record["argv"][1:] == probe_argv and record["exit"] == 0
    assert record["session_id"] == "sess-w" and record["num_turns"] == 3
    assert (out / "stream.jsonl").read_text(encoding="utf-8").count("\n") == len(STREAM) - 1
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "TOOL_USE Read" in summary and "RESULT error" in summary and "FINAL success" in summary

    # a --resume, the budget a metric (issue #115): no cap, no system prompt, no --name
    worker_fake = FakeClaude([STREAM, _result(session_id="sess-a", structured_output=SUMMARY)])
    _worker(tmp_path, worker_fake, binding=False).resume_typed(
        "sess-a", "go on", WorkerOutput, max_turns=7
    )
    probe_fake = FakeClaude([STREAM])
    prompt.write_text("go on", encoding="utf-8", newline="\n")
    code = probe.main(
        ["--prompt-file", str(prompt), "--model", coder_model, "--tools", "coder",
         "--max-turns", "7", "--resume", "sess-a", "--cwd", str(tmp_path / "cwd"),
         "--out", str(tmp_path / "out2")],
        runner=probe_fake,
    )  # fmt: skip
    assert code == 0
    assert probe_fake.calls[0][0] == worker_fake.calls[0][0]
    assert "--max-budget-usd" not in probe_fake.calls[0][0]
    assert not (tmp_path / "out2" / "system.md").exists()


def test_the_probe_defaults_to_a_roles_model_and_a_temporary_cwd_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model ids live only in ``configs/models.yaml``: the probe's default model is the
    ``router`` role's. Its default working directory is a new temporary one, never SCIO."""
    import tempfile

    probe = _load("claude_probe")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))  # no litter in %TEMP%
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8", newline="\n")
    fake = FakeClaude([STREAM])
    code = probe.main(["--prompt-file", str(prompt), "--out", str(tmp_path / "o")], runner=fake)
    assert code == 0
    argv, cwd, _ = fake.calls[0]
    assert cwd is not None and Path(cwd).parent == tmp_path / "tmp"
    router = ModelsConfig.load(Settings().models_path).role("router").model
    assert argv[argv.index("--model") + 1] == router
    assert argv[argv.index("--max-turns") + 1] == str(probe.DEFAULT_MAX_TURNS)
    assert cwd is not None and not Path(cwd).resolve().is_relative_to(REPO_ROOT.resolve())


def test_a_probe_whose_claude_cannot_start_still_writes_its_record(tmp_path: Path) -> None:
    """A missing binary or a timeout leaves the harness's failure kinds; the probe records the
    error and exits 1 instead of dying with a traceback."""
    from scio.harness.failures import FatalError

    def no_binary(argv: list[str], **_: object) -> object:
        raise FatalError("cannot start claude: not found")

    probe = _load("claude_probe")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8", newline="\n")
    out = tmp_path / "o"
    code = probe.main(
        ["--prompt-file", str(prompt), "--cwd", str(tmp_path), "--out", str(out)],
        runner=no_binary,
    )
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert code == 1 and record["exit"] is None and "cannot start" in record["error"]


def test_read_stream_summarizes_tool_uses_results_the_final_result_and_denials() -> None:
    """W-30: the stream-json summary the probe saves and a session reads first."""
    rs = _load("read_stream")
    lines = [json.dumps(e) for e in STREAM]
    final = _result(session_id="sess-d", permission_denials=[{"tool_name": "Bash"}])
    lines += ["Waiting for background work", json.dumps(final)]
    out = rs.summarize(lines)
    assert out[0].startswith("TOOL_USE Read")
    assert any(ln.startswith("RESULT error error: x") for ln in out)
    assert out[-2].startswith("FINAL success") and "session=sess-d" in out[-2]
    assert out[-1] == 'DENIALS [{"tool_name": "Bash"}]'


def test_ledger_pairs_prints_each_call_and_marks_a_resume_that_repeats_its_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-30: one line per accounted call; a resume whose usage repeats its session's previous
    call (whole-session totals, counted twice) is marked."""
    lp = _load("ledger_pairs")
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 300,
             "cache_write_tokens": 40}  # fmt: skip
    rows = [
        {"at": "2026-11-14T10:00:01+00:00", "role": "coder", "usage": usage, "usd": 0.5,
         "latency_ms": 1200, "request_id": "sess-1"},
        {"at": "2026-11-14T10:00:09+00:00", "role": "coder", "usage": usage, "usd": 0.5,
         "latency_ms": 300, "request_id": "sess-1"},
        {"at": "2026-11-14T10:01:00+00:00", "role": "router", "usage": {}, "usd": 0.0,
         "latency_ms": 80, "request_id": None},
    ]  # fmt: skip
    ledger = tmp_path / "t.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    assert lp.main([str(ledger)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "== t.jsonl"
    assert lines[1].startswith("10:00:01 coder") and not lines[1].endswith("REPEAT")
    assert lines[2].endswith("REPEAT") and "lat=    300" in lines[2]
    assert lines[-1] == "total calls=3 usd=1.0000"
