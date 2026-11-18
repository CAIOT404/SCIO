"""``ClaudeCodeWorker``: one worker turn delegated to the unmodified ``claude`` binary
(ADR 0012), with the same surface as :class:`scio.loop.agent_loop.AgentLoop`.

Shape of a turn:

1. ``claude -p`` in the sandbox root with the role's static system prompt (plus a note that
   Claude Code's own tools replace SCIO's), the task projection on stdin, the role's allowed
   tools, ``acceptEdits`` permissions with nobody to answer prompts, a turn cap in Claude
   Code's own unit (:func:`scio.loop.backends.claude_code_max_turns`; never SCIO's tool-call
   cap, I-153) and, only when the budget binds, a dollar cap from the remaining budget
   (issue #115: on the subscription the dollars are a metric); ``stream-json`` output is
   rendered into the same transcript shape ``AgentLoop`` produces, so ``scio report`` reads
   it unchanged.
2. One ``--resume`` call on that session for the typed closing output (``WorkerOutput`` by
   default). It keeps the session's tool list, so it reads the session's prompt cache instead
   of writing it again (issue #185): no ``--tools`` and no ``--json-schema``. The schema
   travels in the prompt, and the reply's JSON is parsed and validated here.
3. Only when the node asks (issue #116, ladder step 1: the session ended at its turn cap and
   git shows no edit): :meth:`ClaudeCodeWorker.resume_typed` continues the same session with
   tools for a bounded number of turns, then takes the typed summary again as in 2.

Verify stays SCIO's ground truth; the ACI's per-edit lint gate does not apply inside a
Claude Code turn. Cost is Claude Code's own estimate, folded into the harness ledger and
budget through :meth:`scio.harness.core.Harness.account`; each call's latency is its own
share of the session's cumulative ``duration_api_ms`` (I-210). Every call runs with
:func:`worker_env`, so the turn's Bash commands find the product's interpreter first (I-114).
The arguments of 1 and 3 come from :func:`worker_argv` and :func:`resume_argv`, which
``scripts/dev/claude_probe.py`` imports, so a probe runs with exactly the worker's flags
(issue #122, W-31); those of 2 from :func:`summary_argv` and its stdin from
:func:`summary_ask`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import with_interpreter_first
from scio.harness.core import Harness
from scio.harness.failures import FatalError
from scio.harness.providers.claude_code import (
    PROVIDER_NAME,
    SAFE_MODE,
    ClaudeRunResult,
    Runner,
    claude_runner,
    parse_result,
    served_model,
    subprocess_env,
    usage_from_result,
)
from scio.harness.types import Message, Part, TextPart, ToolResultPart, ToolUsePart, Usage
from scio.loop.agent_loop import SUMMARY_PROMPT, LoopResult, StopKind
from scio.prompt.registry import PromptRegistry
from scio.prompt.wire_schema import wire_schema
from scio.state.models import WorkerOutput
from scio.toolchain import Toolchain

READ_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
DEFAULT_MAX_TURNS = 30
"""Claude Code turns when no cap is given (T1 and T2 coder and debugger). A turn is one
assistant API message: ``num_turns`` in the ``result`` line, and Claude Code's
``max_turns_reached`` fires after ``max_turns`` of them (I-153)."""
SUMMARY_MAX_TURNS = 3
"""Turns for the typed summary on ``--resume``. Set when the summary used ``--json-schema``,
whose ``StructuredOutput`` tool call Claude Code bounced on a schema miss, and one turn left no
room for the retry (I-077). Since issue #185 the summary keeps the session's tools: a tool the
model still tries costs a turn, denied or not. Measured live (I-250): a plain summary took one
turn, and one told to Write and Read first took all three (the Write denied) and answered."""
SUMMARY_SCHEMA_PROMPT = (
    "Reply with only one JSON object that matches this JSON Schema, with no prose and no code "
    "fence:"
)
"""The summary prompt's last instruction, followed by the schema as JSON (issue #185)."""

BACKEND_NOTE = (
    "\n\nThis turn runs inside Claude Code. Its built-in tools replace the ones named above: "
    "Grep and Glob for repo_search, Read for view_file, Edit and Write for edit_file, and "
    "Bash for run_tests ({test_cmd}). Dependencies are installed by the orchestrator from the "
    "product's approved list before your turn; never install packages yourself, ask for a "
    "design decision instead. When the product has an interpreter of its own, `python` "
    "already runs the product's environment with its approved dependencies installed; never "
    "look for another interpreter. Keep every path inside the repository root, never run git "
    "commands that change history, and stop when the tests you were asked for pass."
)
"""Appended to the role's system prompt. The interpreter sentence is I-114: a live coder spent
36 tool calls hunting for an interpreter that could import a product dependency."""


def worker_env(sandbox: Sandbox, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a worker turn's ``claude -p`` process (I-114).

    :func:`subprocess_env`, plus the product interpreter's directory first on PATH and
    ``VIRTUAL_ENV`` at that interpreter's environment root, so the turn's Bash commands run
    the product's interpreter with its approved dependencies, not SCIO's own (which is first
    on SCIO's PATH under ``uv run``). SCIO's directories stay on PATH after it for the
    profile's other tools. Unchanged when the sandbox has no interpreter of its own.
    """
    return with_interpreter_first(subprocess_env(base), getattr(sandbox, "python", None))


def allowed_tools_for(tool_names: tuple[str, ...], toolchain: Toolchain) -> list[str]:
    """Claude Code permission rules equivalent to a SCIO toolbox."""
    rules: list[str] = []
    if {"repo_search", "view_file"} & set(tool_names):
        rules += READ_TOOLS
    if "edit_file" in tool_names:
        rules += EDIT_TOOLS
    if "delete_file" in tool_names:
        rules += ["Bash(rm *)"]  # Claude Code has no delete tool; rm inside the sandbox root
    if "run_app" in tool_names:
        rules += [f"Bash({' '.join(toolchain.runtime().app_argv)} *)"]
    # install_deps has no rule on purpose: the install phase runs outside the turn (M9)
    if "run_tests" in tool_names:
        # The profile's test and lint commands (``python -m pytest`` is ``python`` already;
        # ``node --test`` is ``node``), nothing stack-specific here (M10).
        test_bin = toolchain.test_argv(filter=None, path=None)[0]
        rules += [f"Bash({test_bin} *)", f"Bash({toolchain.lint_argv('.')[0]} *)"]
    return list(dict.fromkeys(rules))


def session_name(role: str) -> str:
    """The ``--name`` of a worker session for ``role``."""
    return f"scio-{role}"


def worker_system_prompt(prompts: PromptRegistry, role: str, toolchain: Toolchain) -> str:
    """The system prompt of a worker turn: the role's static prompt plus :data:`BACKEND_NOTE`
    naming the toolchain's test command."""
    test_cmd = " ".join(toolchain.test_argv(filter=None, path=None))
    return prompts.get(role).render() + BACKEND_NOTE.format(test_cmd=test_cmd)


def _tool_turn_argv(
    *,
    model: str,
    session: list[str],
    allowed_tools: Sequence[str],
    max_turns: int,
    budget_cap: float | None,
    name: list[str],
) -> list[str]:
    cap = [] if budget_cap is None else ["--max-budget-usd", f"{max(budget_cap, 0.01):.2f}"]
    return [
        "-p",
        SAFE_MODE,  # never the machine's CLAUDE.md (I-171)
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        *session,
        "--allowedTools",
        ",".join(allowed_tools),
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--max-turns",
        str(max_turns),
        *cap,
        *name,
    ]


def worker_argv(
    *,
    model: str,
    system_path: Path | str,
    allowed_tools: Sequence[str],
    max_turns: int,
    budget_cap: float | None,
    name: str,
) -> list[str]:
    """The ``claude`` arguments (binary excluded) of a tool-using worker turn that starts a new
    session: :meth:`ClaudeCodeWorker.run_typed` passes exactly these, and so does
    ``scripts/dev/claude_probe.py`` (issue #122, W-31).

    ``allowed_tools`` are Claude Code permission rules (:func:`allowed_tools_for`).
    ``budget_cap`` is the remaining budget in dollars when the budget binds, else None: a
    budget that does not bind never caps or shortens a session (issue #115). ``name`` is the
    session's ``--name`` (:func:`session_name`).
    """
    return _tool_turn_argv(
        model=model,
        session=["--system-prompt-file", str(system_path)],
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        budget_cap=budget_cap,
        name=["--name", name],
    )


def resume_argv(
    *,
    model: str,
    session_id: str,
    allowed_tools: Sequence[str],
    max_turns: int,
    budget_cap: float | None,
) -> list[str]:
    """The ``claude`` arguments of a tool-using ``--resume`` of ``session_id``
    (:meth:`ClaudeCodeWorker.resume_typed`, issue #116). Every flag of :func:`worker_argv` is
    passed again: ``--permission-mode`` is not inherited on a ``-p`` resume (sessions docs,
    measured on issue #116). The session keeps its system prompt and its ``--name``, so neither
    is sent again."""
    return _tool_turn_argv(
        model=model,
        session=["--resume", session_id],
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        budget_cap=budget_cap,
        name=[],
    )


def summary_argv(*, model: str, session_id: str) -> list[str]:
    """The ``claude`` arguments of the typed summary that closes a worker turn: a ``--resume``
    of ``session_id`` that keeps the session's tool list, so it reads the session's prompt
    cache (issue #185, I-250). Tool definitions are part of the cached prefix: ``--tools ""``
    removed them and ``--json-schema`` added a ``StructuredOutput`` tool, and either made the
    call write the whole context to the cache again. The schema travels in the prompt instead
    (:func:`summary_ask`).

    No tool may edit: there is no ``--allowedTools`` and no ``acceptEdits``. The mode is
    passed, never inherited: a ``-p --resume`` starts in the mode a new ``-p`` run would,
    which is the settings' ``permissions.defaultMode``, and on the owner's machine that is
    ``auto``, where the live probe's Write ran (I-250). ``dontAsk`` denies every call that
    would prompt; reads inside the working directory and read-only commands still run."""
    return [
        "-p",
        SAFE_MODE,  # never the machine's CLAUDE.md (I-171)
        "--output-format",
        "json",
        "--resume",
        session_id,
        "--model",
        model,
        "--max-turns",
        str(SUMMARY_MAX_TURNS),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
    ]


def summary_ask(prompt: str, schema: Mapping[str, Any], error: str = "") -> str:
    """The summary call's stdin: ``prompt``, then the previous reply's ``error`` when it is
    asked again, then :data:`SUMMARY_SCHEMA_PROMPT` and ``schema`` as JSON, last."""
    parts = [prompt]
    if error:
        parts.append(f"Your previous reply was invalid: {error}")
    parts.append(f"{SUMMARY_SCHEMA_PROMPT}\n{json.dumps(schema)}")
    return "\n\n".join(parts)


def summary_reply_json(text: str) -> Any:
    """The JSON value of a summary reply: ``text`` with one surrounding code fence (three
    backticks, optionally followed by ``json``) removed, then parsed. A reply that is not
    JSON raises ``json.JSONDecodeError``."""
    body = text.strip()
    if len(body) >= 6 and body.startswith("```") and body.endswith("```"):
        body = body[3:-3]
        if body[:4].lower() == "json":
            body = body[4:]
        body = body.strip()
    return json.loads(body)


@dataclass
class StreamTurn:
    """What one ``stream-json`` run produced."""

    messages: list[Message] = field(default_factory=list)
    tool_calls: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    turns: int = 0


def parse_stream(stdout: str) -> StreamTurn:
    """Render ``stream-json`` lines into SCIO messages; the last ``result`` line is kept."""
    turn = StreamTurn()
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "result":
            turn.result = event
            turn.turns = int(event.get("num_turns") or 0)
            continue
        if kind not in ("assistant", "user"):
            continue
        if event.get("parent_tool_use_id"):
            continue  # subagent traffic is not this worker's transcript
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        parts: list[Part] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(TextPart(text=str(block["text"])))
            elif btype == "tool_use":
                turn.tool_calls += 1
                parts.append(
                    ToolUsePart(
                        id=str(block.get("id") or f"call-{turn.tool_calls}"),
                        name=str(block.get("name") or "tool"),
                        input=dict(block.get("input") or {}),
                    )
                )
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    text = "\n".join(str(b.get("text", "")) for b in inner if isinstance(b, dict))
                else:
                    text = str(inner or "")
                parts.append(
                    ToolResultPart(
                        tool_use_id=str(block.get("tool_use_id") or ""),
                        content=text[:4000],
                        is_error=bool(block.get("is_error")),
                    )
                )
        if parts:
            turn.messages.append(
                Message(role="assistant" if kind == "assistant" else "user", parts=parts)
            )
    return turn


def validate_with_trimming[T: BaseModel](output_model: type[T], raw: Any) -> T:
    """Validate ``raw`` against ``output_model``, trimming string fields that only exceed
    their ``max_length`` (I-070). The wire schema in the summary prompt carries no length
    caps, and a cap on prose is a presentation limit: trim, then validate again.
    Any other error is raised unchanged. ``raw`` is modified in place when trimmed."""
    try:
        return output_model.model_validate(raw)
    except ValidationError as exc:
        if not isinstance(raw, dict):
            raise
        trimmed = False
        for err in exc.errors():
            if err.get("type") != "string_too_long":
                continue
            loc = err.get("loc") or ()
            limit = (err.get("ctx") or {}).get("max_length")
            if len(loc) != 1 or not isinstance(limit, int):
                continue
            key = loc[0]
            if isinstance(raw.get(key), str):
                raw[key] = raw[key][: max(limit - 3, 0)].rstrip() + "..."
                trimmed = True
        if not trimmed:
            raise
        return output_model.model_validate(raw)


class ClaudeCodeWorker:
    def __init__(
        self,
        harness: Harness,
        sandbox: Sandbox,
        prompts: PromptRegistry,
        role: str,
        *,
        toolchain: Toolchain,
        tool_names: tuple[str, ...],
        max_turns: int | None = None,
        binary: str = "claude",
        runner: Runner | None = None,
        timeout_s: float = 1800.0,
    ) -> None:
        self.harness = harness
        self.sandbox = sandbox
        self.prompts = prompts
        self.role = role
        self.toolchain = toolchain
        self.tool_names = tool_names
        self.max_turns = max_turns or DEFAULT_MAX_TURNS
        self.binary = binary
        self._run = runner or claude_runner(binary)
        self.timeout_s = timeout_s
        self.calls: list[list[str]] = []
        self._api_ms: dict[str, int] = {}
        """Each session's cumulative ``duration_api_ms`` after its last call (I-210)."""

    def run(self, user_message: str, *, tags: dict[str, str] | None = None) -> LoopResult:
        return self.run_typed(user_message, WorkerOutput, tags=tags)

    def run_typed[T: BaseModel](
        self,
        user_message: str,
        output_model: type[T],
        *,
        summary_prompt: str = SUMMARY_PROMPT,
        tags: dict[str, str] | None = None,
    ) -> LoopResult:
        model = self.harness.config.role(self.role).model
        system = worker_system_prompt(self.prompts, self.role, self.toolchain)
        # Built per turn, not in __init__: the install phase sets ``sandbox.python`` after the
        # worker exists (a resumed build, a lazy install_deps).
        env = worker_env(self.sandbox)
        with tempfile.TemporaryDirectory(prefix="scio-claude-") as tmp:
            system_path = Path(tmp) / "system.md"
            system_path.write_text(system, encoding="utf-8", newline="\n")
            argv = worker_argv(
                model=model,
                system_path=system_path,
                allowed_tools=allowed_tools_for(self.tool_names, self.toolchain),
                max_turns=self.max_turns,
                budget_cap=self._budget_cap(),
                name=session_name(self.role),
            )
            turn, stop, session_id = self._stream(
                argv, user_message, self.max_turns, model, tags, env
            )
        return self._close(
            turn, stop, session_id, user_message, output_model, summary_prompt, model, tags, env
        )

    def resume_typed[T: BaseModel](
        self,
        session_id: str,
        prompt: str,
        output_model: type[T],
        *,
        max_turns: int,
        summary_prompt: str = SUMMARY_PROMPT,
        tags: dict[str, str] | None = None,
    ) -> LoopResult:
        """Ladder step 1 (issue #116): continue ``session_id`` with its tools for at most
        ``max_turns`` more turns, then take the typed summary again, as :meth:`run_typed`
        does. Measured live on Claude Code 2.1.325: a ``-p --resume`` keeps the session id and
        its system prompt, runs tools when the tool flags are passed again (also after the
        summary call, then made with no tools and ``--json-schema``), and counts
        ``--max-turns`` from zero."""
        model = self.harness.config.role(self.role).model
        env = worker_env(self.sandbox)
        argv = resume_argv(
            model=model,
            session_id=session_id,
            allowed_tools=allowed_tools_for(self.tool_names, self.toolchain),
            max_turns=max_turns,
            budget_cap=self._budget_cap(),
        )
        turn, stop, sid = self._stream(argv, prompt, max_turns, model, tags, env)
        return self._close(
            turn, stop, sid or session_id, prompt, output_model, summary_prompt, model, tags, env
        )

    def _budget_cap(self) -> float | None:
        """The remaining dollars for ``--max-budget-usd``, read at call time (a resume sees what
        the session already spent); None when the budget does not bind (issue #115)."""
        guard = self.harness.budget
        if not guard.binding:
            return None
        snap = guard.snapshot()
        return snap.usd_max - snap.usd_used

    def _stream(
        self,
        argv: list[str],
        stdin: str,
        max_turns: int,
        model: str,
        tags: dict[str, str] | None,
        env: Mapping[str, str],
    ) -> tuple[StreamTurn, StopKind, str]:
        """Run one ``stream-json`` call and account it: its turn, its stop, its session id."""
        self.calls.append(list(argv))
        res = self._run(argv, cwd=self.sandbox.root, stdin=stdin, timeout_s=self.timeout_s, env=env)
        turn = parse_stream(res.stdout)
        if not turn.result:
            raise FatalError(
                f"claude -p ({self.role}) produced no result: exit {res.code}, "
                f"{(res.stderr or res.stdout).strip()[:400]!r}"
            )
        capped = turn.result.get("subtype") == "error_max_turns"
        if capped:  # the turn cap is a normal stop for SCIO, not an error: summarise the session
            turn.result = {**turn.result, "is_error": False}
        result = parse_result(ClaudeRunResult(res.code, json.dumps(turn.result), res.stderr))
        self._account(result, model, tags)
        stop: StopKind = "tool_cap" if capped or turn.turns >= max_turns else "end_turn"
        return turn, stop, str(result.get("session_id") or "")

    def _close[T: BaseModel](
        self,
        turn: StreamTurn,
        stop: StopKind,
        session_id: str,
        stdin: str,
        output_model: type[T],
        summary_prompt: str,
        model: str,
        tags: dict[str, str] | None,
        env: Mapping[str, str],
    ) -> LoopResult:
        """The typed closing output: one more call on the same session, with its tool list
        unchanged and no tool allowed (:func:`summary_argv`)."""
        messages = [Message.user(stdin), *turn.messages]
        schema = wire_schema(output_model)
        output, summary_text, calls = self._summary(
            session_id, schema, output_model, summary_prompt, model, tags, env
        )
        messages.append(Message.assistant(summary_text))
        return LoopResult(
            output=output,
            stop=stop,
            tool_calls=turn.tool_calls,
            model_calls=max(turn.turns, 1) + calls,
            messages=messages,
            session_id=session_id or None,
        )

    def _summary[T: BaseModel](
        self,
        session_id: str,
        schema: dict[str, Any],
        output_model: type[T],
        prompt: str,
        model: str,
        tags: dict[str, str] | None,
        env: Mapping[str, str],
    ) -> tuple[T, str, int]:
        last_error = ""
        argv = summary_argv(model=model, session_id=session_id)
        for attempt in range(1, 3):
            self.calls.append(list(argv))
            res = self._run(
                argv,
                cwd=self.sandbox.root,
                stdin=summary_ask(prompt, schema, last_error),
                timeout_s=self.timeout_s,
                env=env,
            )
            try:
                data = parse_result(res)
            except FatalError as exc:
                if "error_max_turns" not in str(exc):
                    raise
                # No reply within the turn cap (I-077; since #185 a denied tool attempt
                # spends a turn): re-ask once with the schema's required keys spelled out.
                last_error = (
                    "the reply never matched the schema within the turn cap; use no tool; "
                    f"required keys: {', '.join(schema.get('required', []))}"
                )
                continue
            self._account(data, model, tags)
            # Claude Code sets ``structured_output`` only for a ``--json-schema`` call, which
            # the summary no longer makes (#185); a result that carries one is still taken.
            payload = data.get("structured_output")
            try:
                raw = (
                    payload
                    if payload is not None
                    else summary_reply_json(str(data.get("result") or ""))
                )
                value = validate_with_trimming(output_model, raw)
                return value, json.dumps(raw), attempt
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = str(exc)[:600]
        raise FatalError(
            f"claude -p ({self.role}) summary never matched {output_model.__name__}: {last_error}"
        )

    def _account(self, data: dict[str, Any], model: str, tags: dict[str, str] | None) -> Usage:
        usage = usage_from_result(data)
        session_id = str(data.get("session_id") or "")
        self.harness.account(
            role=self.role,
            model=served_model(data, model),
            provider=PROVIDER_NAME,
            usage=usage,
            latency_ms=self._own_latency(session_id, int(data.get("duration_api_ms") or 0)),
            request_id=session_id or None,
            tags=tags,
        )
        return usage

    def _own_latency(self, session_id: str, cumulative_ms: int) -> int:
        """This call's own API time (I-210). ``duration_api_ms`` on a resumed call covers the
        whole session (measured on issue #116), so the summary call had recorded the session's
        time a second time: the session's figure after its previous call is subtracted. The
        ``usage`` block is per call already, so the dollars need no such correction."""
        if not session_id:
            return cumulative_ms
        before = self._api_ms.get(session_id, 0)
        self._api_ms[session_id] = max(before, cumulative_ms)
        return max(cumulative_ms - before, 0)
