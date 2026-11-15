"""One ``claude -p`` probe with exactly the flags SCIO's Claude Code worker passes.

Usage::

    uv run python scripts/dev/claude_probe.py --prompt-file prompt.md [--tools coder]
        [--model router] [--max-turns 5] [--resume SESSION] [--cwd DIR] [--out DIR]

A question about Claude Code's behaviour (does ``--resume`` keep the tools, which turn counts
toward ``--max-turns``, what a denial looks like) is answered by a probe, never from memory
(issue #122, W-31). The arguments come from :func:`scio.loop.claude_code_worker.worker_argv`
and :func:`~scio.loop.claude_code_worker.resume_argv`, the functions
``ClaudeCodeWorker.run_typed`` and ``resume_typed`` call, so a probe cannot drift from the
worker. Only the system prompt file's path differs: the worker writes it to a temporary
directory, the probe to ``--out``.

- ``--tools`` names a role whose toolbox the probe gets (``coder``, ``debugger``,
  ``documenter``, ``explorer``); the role's system prompt is rendered as the worker renders it,
  unless ``--system-file`` replaces it.
- ``--model`` is a role in ``configs/models.yaml`` or a model id. The default is the
  ``router`` role's model (Haiku): model ids live only in ``configs/models.yaml``.
- ``--budget-usd`` passes ``--max-budget-usd`` as a binding budget does; without it no cap is
  passed, as on the subscription (issue #115).
- ``claude`` always runs without ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` and
  ``CLAUDECODE`` (:func:`scio.harness.providers.claude_code.subprocess_env`), so the login
  bills, never a stale key, and print mode starts inside a Claude Code session.
- ``--cwd`` defaults to a new temporary directory outside the repository, so the probe never
  edits SCIO and no CLAUDE.md applies (the worker passes ``--safe-mode`` anyway).

``--out`` (default ``.scio/probes/<UTC stamp>/``) receives ``stream.jsonl``, ``stderr.txt``,
``system.md`` (a new session only), ``summary.txt`` (``read_stream.py``'s summary) and
``record.json``: the argv, the exit code, the session id, the result's subtype, turns, cost
and durations, and the wall time.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from scio.aci.sandbox.local import with_interpreter_first
from scio.graph.nodes.implement import WRITER_TOOLS
from scio.graph.nodes.planning import EXPLORER_TOOLS
from scio.graph.nodes.worker import DOCUMENTER_TOOLS
from scio.harness.config import ModelsConfig
from scio.harness.failures import FatalError, RetryableError
from scio.harness.providers.claude_code import (
    STRIPPED_ENV,
    Runner,
    claude_runner,
    subprocess_env,
)
from scio.loop.claude_code_worker import (
    allowed_tools_for,
    resume_argv,
    session_name,
    worker_argv,
    worker_system_prompt,
)
from scio.prompt.registry import PromptRegistry
from scio.settings import REPO_ROOT, Settings
from scio.toolchain import available_toolchains, get_toolchain

ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "coder": WRITER_TOOLS,
    "debugger": WRITER_TOOLS,
    "documenter": DOCUMENTER_TOOLS,
    "explorer": EXPLORER_TOOLS,
}
"""The toolbox each worker role gets in the graph (``scio/graph/nodes``)."""
DEFAULT_MODEL_ROLE = "router"
DEFAULT_MAX_TURNS = 5
"""A probe asks one question; the worker's own cap is the role's (``backends.py``)."""


def _read_stream() -> ModuleType:
    """``read_stream.py`` beside this file, loaded by path: tests load this script with
    ``spec_from_file_location``, so its directory is not on ``sys.path``."""
    path = Path(__file__).with_name("read_stream.py")
    spec = importlib.util.spec_from_file_location("scio_dev_read_stream", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_model(value: str, config: ModelsConfig) -> str:
    """A role's model when ``value`` names a role, else ``value`` as a model id."""
    role = config.roles.get(value)
    return role.model if role is not None else value


def probe_argv(args: argparse.Namespace, system_path: Path | None, model: str) -> list[str]:
    """The ``claude`` arguments for this probe, from the worker's own argv functions."""
    rules = allowed_tools_for(ROLE_TOOLS[args.tools], get_toolchain(args.stack))
    if args.resume:
        return resume_argv(
            model=model,
            session_id=args.resume,
            allowed_tools=rules,
            max_turns=args.max_turns,
            budget_cap=args.budget_usd,
        )
    assert system_path is not None
    return worker_argv(
        model=model,
        system_path=system_path,
        allowed_tools=rules,
        max_turns=args.max_turns,
        budget_cap=args.budget_usd,
        name=session_name(args.tools),
    )


def _last_result(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--prompt-file", required=True, help="the user message, sent on stdin")
    p.add_argument("--model", default=DEFAULT_MODEL_ROLE, help="a role or a model id")
    p.add_argument("--tools", default="coder", choices=sorted(ROLE_TOOLS))
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--resume", default=None, metavar="SESSION")
    p.add_argument("--cwd", default=None, help="default: a new temporary directory")
    p.add_argument("--out", default=None, help="default: .scio/probes/<UTC stamp>/")
    p.add_argument("--system-file", default=None, help="replaces the role's system prompt")
    p.add_argument("--budget-usd", type=float, default=None, help="passes --max-budget-usd")
    p.add_argument("--stack", default="python", choices=available_toolchains())
    p.add_argument("--python", default=None, help="a product interpreter, first on PATH")
    p.add_argument("--binary", default=None, help="default: Settings.claude_code_bin")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds")
    return p


def main(argv: list[str] | None = None, *, runner: Runner | None = None) -> int:
    """Run one probe; 0 when ``claude`` exited 0. ``runner`` replaces the real ``claude``
    (tests)."""
    args = _parser().parse_args(argv)
    settings = Settings()
    model = resolve_model(args.model, ModelsConfig.load(settings.models_path))
    started = datetime.now(UTC)
    out = (
        Path(args.out)
        if args.out
        else REPO_ROOT / ".scio" / "probes" / started.strftime("%Y%m%dT%H%M%SZ")
    )
    out.mkdir(parents=True, exist_ok=True)
    cwd = Path(args.cwd) if args.cwd else Path(tempfile.mkdtemp(prefix="scio-probe-"))
    cwd.mkdir(parents=True, exist_ok=True)
    system_path: Path | None = None
    if not args.resume:  # a resumed session keeps its system prompt
        system = (
            Path(args.system_file).read_text(encoding="utf-8")
            if args.system_file
            else worker_system_prompt(PromptRegistry(), args.tools, get_toolchain(args.stack))
        )
        system_path = out / "system.md"
        system_path.write_text(system, encoding="utf-8", newline="\n")
    claude_args = probe_argv(args, system_path, model)
    binary = args.binary or settings.claude_code_bin
    # worker_env's environment: subprocess_env (no API key, no CLAUDECODE), plus the product
    # interpreter first on PATH when one is given (I-114).
    env = with_interpreter_first(subprocess_env(), args.python)
    run = runner or claude_runner(binary)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    code: int | None = None
    stdout = stderr = error = ""
    t0 = time.monotonic()
    try:
        res = run(claude_args, cwd=cwd, stdin=prompt, timeout_s=args.timeout, env=env)
        code, stdout, stderr = res.code, res.stdout, res.stderr
    except (FatalError, RetryableError) as exc:  # a timeout, or no binary
        error = str(exc)
    wall_s = time.monotonic() - t0
    (out / "stream.jsonl").write_text(stdout, encoding="utf-8", newline="\n")
    (out / "stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
    summary = _read_stream().summarize(stdout.splitlines())
    (out / "summary.txt").write_text(
        "".join(f"{line}\n" for line in summary), encoding="utf-8", newline="\n"
    )
    result = _last_result(stdout)
    record = {
        "argv": [binary, *claude_args],
        "cwd": str(cwd),
        "model": model,
        "tools": args.tools,
        "resume": args.resume,
        "env_removed": list(STRIPPED_ENV),
        "exit": code,
        "error": error or None,
        "session_id": result.get("session_id"),
        "subtype": result.get("subtype"),
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "permission_denials": result.get("permission_denials"),
        "started_at": started.isoformat(timespec="seconds"),
        "ended_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "wall_s": round(wall_s, 2),
    }
    (out / "record.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    for line in summary:
        print(line)
    if error:
        print(f"ERROR {error}")
    print(f"probe: exit {code}, session {record['session_id']}, {wall_s:.1f}s; {out}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a model's text is not cp1252
    sys.exit(main())
