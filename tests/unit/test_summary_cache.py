"""Issue #185 (I-250): the worker's summary resume reads the session's prompt cache.

Every Claude Code worker session ends with a ``--resume`` call for the typed summary. Its
``--tools ""`` and ``--json-schema`` (which adds a ``StructuredOutput`` tool) changed the
session's tool list, and tool definitions are part of the cached prefix, so the call wrote
the whole context to the cache again: the audit's probe read 0 and wrote 12,790 tokens on the
exact old argv, about 15% of a feature's cost. The summary now keeps the session's tool list,
denies every tool call that would prompt, carries the schema in its prompt and parses the
reply's JSON itself. Live probe (I-250): a plain summary, before ``--permission-mode dontAsk``
was added, read 28,144 and wrote 1,078; the final argv, told to Write first, read 62,698 and
wrote 1,452, and the Write was denied.

Only names that existed before #185 are imported at module level, so on the old code each
test fails at its own assertion, not at import.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.failures import FatalError
from scio.harness.providers.claude_code import ClaudeRunResult
from scio.loop.agent_loop import SUMMARY_PROMPT
from scio.loop.claude_code_worker import ClaudeCodeWorker
from scio.prompt.registry import PromptRegistry
from scio.prompt.wire_schema import wire_schema
from scio.settings import Settings
from scio.state.models import WorkerOutput
from scio.toolchain import python_toolchain

SCHEMA_LINE = (
    "Reply with only one JSON object that matches this JSON Schema, with no prose and no code "
    "fence:"
)
"""The prompt's last instruction, in the words of the decided design (#185)."""

SUMMARY: dict[str, Any] = {
    "step_id": "S1", "summary": "added slugify", "files_touched": ["a.py"],
    "confidence": "high", "needs_design_decision": False, "design_question": None,
    "next_action": "verify", "tests_added": ["tests/test_a.py::test_slugify"], "sweep": None,
}  # fmt: skip


class FakeClaude:
    """Canned ``claude -p`` runs in call order; records each call's argv and stdin."""

    def __init__(self, results: list[dict[str, Any] | list[dict[str, Any]]]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float = 0.0,
        env: Mapping[str, str] | None = None,
    ) -> ClaudeRunResult:
        self.calls.append((list(argv), stdin or ""))
        nxt = self.results.pop(0)
        if isinstance(nxt, list):  # stream-json lines
            return ClaudeRunResult(0, "\n".join(json.dumps(e) for e in nxt), "")
        return ClaudeRunResult(0, json.dumps(nxt), "")


def _result(text: str, **extra: Any) -> dict[str, Any]:
    """A ``--output-format json`` result whose reply text is ``text``."""
    base: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": "sess-w",
        "total_cost_usd": 0.01,
        "duration_api_ms": 1200,
        "num_turns": 1,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 114,
            "cache_read_input_tokens": 28_144,
            "cache_creation_input_tokens": 1_078,
        },
    }
    base.update(extra)
    return base


READ = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}
STREAM: list[dict[str, Any]] = [
    {"type": "system", "subtype": "init", "session_id": "sess-w"},
    {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [READ]}},
    _result("Done.", num_turns=3),
]


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _run(tmp_path: Path, *replies: dict[str, Any]) -> tuple[FakeClaude, Harness, Any]:
    """One coder turn and its summary; returns the fake, the harness and the LoopResult."""
    settings = Settings(provider_override="fake")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(config, ProviderRegistry(config, settings), settings=settings)
    fake = FakeClaude([STREAM, *replies])
    worker = ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("repo_search", "view_file", "edit_file"),
        runner=fake,
    )  # fmt: skip
    return fake, harness, worker.run("# Task: add slugify")


def test_the_summary_resume_keeps_the_sessions_tool_list(tmp_path: Path) -> None:
    """The owner's goal: "The summary resume keeps the session's tool list: no ``--tools``,
    no ``--json-schema``." Either flag changes the tool definitions in the cached prefix."""
    fake, _, _ = _run(tmp_path, _result(json.dumps(SUMMARY)))
    (first, _), (summary, _) = fake.calls
    assert _flag(summary, "--resume") == "sess-w"
    for argv in (first, summary):  # neither call narrows or extends the built-in tools
        assert "--tools" not in argv and "--json-schema" not in argv
    assert "--system-prompt-file" not in summary  # the session keeps its system prompt
    assert _flag(summary, "--model") == _flag(first, "--model")
    assert "--safe-mode" in summary  # never the machine's CLAUDE.md (I-171)


def test_the_summary_denies_tools_without_a_prompt(tmp_path: Path) -> None:
    """The owner's goal: "A tool attempt in it is denied, never prompted." No rule allows a
    tool and no mode accepts edits. The mode is explicit: a ``-p --resume`` starts in the
    settings' ``defaultMode``, ``auto`` on the owner's machine, where the probe's Write ran;
    with ``dontAsk`` the same Write was denied (I-250)."""
    fake, _, _ = _run(tmp_path, _result(json.dumps(SUMMARY)))
    summary = fake.calls[1][0]
    assert _flag(summary, "--permission-prompts") == "none"
    assert _flag(summary, "--permission-mode") == "dontAsk"
    assert "acceptEdits" not in summary and "--allowedTools" not in summary
    assert not any(arg.startswith("--allowed") for arg in summary)


def test_the_summary_schema_is_in_the_prompt_and_its_reply_is_parsed(tmp_path: Path) -> None:
    """The owner's goal: "The schema travels in the prompt, and the reply's JSON is parsed
    and validated." The prompt ends with the instruction and the wire schema; a reply in a
    ``json`` code fence is still parsed. The ledger accounts the call as before."""
    fenced = "```json\n" + json.dumps(SUMMARY, indent=2) + "\n```"
    fake, harness, result = _run(tmp_path, _result(fenced))
    stdin = fake.calls[1][1]
    assert stdin.startswith(SUMMARY_PROMPT)
    assert stdin.endswith(f"{SCHEMA_LINE}\n{json.dumps(wire_schema(WorkerOutput))}")
    assert result.output == WorkerOutput.model_validate(SUMMARY)
    assert result.transcript().endswith(json.dumps(SUMMARY))
    entries = harness.ledger.to_dict()["entries"]
    assert len(entries) == 2 and harness.budget.snapshot().calls == 2
    summary_row = entries[1]
    assert summary_row["provider"] == "claude_code" and summary_row["role"] == "coder"


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps(SUMMARY),
        "  " + json.dumps(SUMMARY) + "\n",
        "```json\n" + json.dumps(SUMMARY) + "\n```",
        "```JSON\n" + json.dumps(SUMMARY) + "\n```",
        "```\n" + json.dumps(SUMMARY) + "\n```",
        "```json" + json.dumps(SUMMARY) + "```",
    ],
    ids=["bare", "whitespace", "json-fence", "upper-fence", "plain-fence", "one-line-fence"],
)
def test_a_summary_reply_parses_with_or_without_one_code_fence(reply: str) -> None:
    from scio.loop.claude_code_worker import summary_reply_json

    assert summary_reply_json(reply) == SUMMARY


def test_a_summary_reply_with_prose_is_not_json() -> None:
    from scio.loop.claude_code_worker import summary_reply_json

    with pytest.raises(json.JSONDecodeError):
        summary_reply_json("Here is the summary:\n```json\n{}\n```")


def test_an_invalid_summary_reply_is_asked_again_once_with_the_error(tmp_path: Path) -> None:
    """The owner's goal: "with one re-ask on an invalid reply, as today." The re-ask carries
    the error and still ends with the schema; a second invalid reply raises ``FatalError``
    as before."""
    prose = _result("I added slugify and the tests pass.")
    fake, _, result = _run(tmp_path, prose, _result(json.dumps(SUMMARY)))
    assert result.output == WorkerOutput.model_validate(SUMMARY) and len(fake.calls) == 3
    (_, first_ask), (again_argv, again) = fake.calls[1], fake.calls[2]
    assert again_argv == fake.calls[1][0]  # the same argv: the tool list is unchanged again
    assert "Your previous reply was invalid:" in again and "Expecting value" in again
    assert again.startswith(SUMMARY_PROMPT) and "previous reply" not in first_ask
    assert again.endswith(f"{SCHEMA_LINE}\n{json.dumps(wire_schema(WorkerOutput))}")

    missing = _result(json.dumps({"step_id": "S1"}))  # valid JSON, missing required fields
    with pytest.raises(FatalError, match="summary never matched WorkerOutput"):
        _run(tmp_path, prose, missing)
