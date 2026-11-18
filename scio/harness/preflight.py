"""The credential pre-flight of a live product command (issue #103).

A live ``scio new`` or ``scio build`` on the Claude Code backend (ADR 0012) checks its
sign-in with one cheap call before it writes anything: no thread, checkpoint or product
file. A missing or expired sign-in is then one line and exit 2, before any spend, instead of
a harness stop in the middle of a milestone (I-060 named the problem on the API profile).

The call is ``claude -p`` built as :class:`~scio.harness.providers.claude_code.ClaudeCodeProvider`
builds a tool-less call: ``--safe-mode``, no tools, one turn, ``--output-format json``, no
session persistence, and a one-line prompt on stdin. It names no ``--model``: the probe ran
on the account's default model, the router role's cheaper model has never run through
``claude -p`` here (the live ledgers hold the Opus and Sonnet roles only), and a model the
sign-in could not use would refuse every live command. It goes through the same injectable
runner, so a test never runs a live ``claude``.

What ``claude -p`` prints signed out (probed on Claude Code 2.1.327 with an empty
``CLAUDE_CONFIG_DIR``): exit 1, and on stdout one JSON result with ``"is_error": true``,
``"subtype": "success"`` and ``"result": "Not logged in · Please run /login"``. Signed in it
exits 0 with ``"is_error": false``, and prints nothing on stderr.
"""

from __future__ import annotations

import json
from typing import Any

from scio.harness.failures import HarnessError
from scio.harness.providers.claude_code import SAFE_MODE, ClaudeRunResult, Runner
from scio.settings import Settings

PREFLIGHT_PROMPT = "Reply with OK."
PREFLIGHT_TIMEOUT_S = 60.0
"""A pre-flight that has not answered in this time is :class:`PreflightFailed`."""

SIGN_IN_REQUIRED_PREFIX = "scio: sign-in required:"
"""How a sign-in refusal's line starts. The recovery supervisor reads it as
``external_auth`` (``AUTH_MARKERS`` in ``recovery/classify.py``): it escalates and never
starts a repair. The two packages share this text, never code."""

PREFLIGHT_FAILED_PREFIX = "scio: pre-flight check failed:"
"""How the line of any other pre-flight failure starts: a timeout, a ``claude`` that cannot
start, a rate limit. It is never a sign-in line, so the supervisor never reads it as
``external_auth``; its reason decides (a timeout or a usage window is transient)."""

SIGN_IN_MARKERS: tuple[str, ...] = (
    "not logged in",
    "/login",
    "invalid_grant",
    "oauth token",
    "authentication",
)
"""Text that says the sign-in failed, matched case-insensitively. The first two are what the
probe printed signed out ("Not logged in · Please run /login"); the rest are the auth
markers the provider (``FATAL_ERRORS``) and the supervisor (``AUTH_MARKERS``) already read.
The middle dot is never matched: a non-UTF-8 stdout may have replaced it."""

LINE_LIMIT = 300


class PreflightError(Exception):
    """The pre-flight refused the command. ``reason`` is one line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SignInRequired(PreflightError):
    """``claude`` answered that it is not signed in, or that its sign-in failed."""


class PreflightFailed(PreflightError):
    """The pre-flight failed for another reason: a timeout, a ``claude`` that cannot start,
    an error result that names no sign-in failure, or output that is not a result."""


def preflight_applies(settings: Settings) -> bool:
    """Whether a live command runs the pre-flight: only when its tool-less calls go through
    the ``claude`` CLI (``SCIO_PROVIDER_OVERRIDE=claude_code``). The Fake provider and an
    API provider skip it."""
    return settings.provider_override == "claude_code"


def preflight_argv(model: str | None = None) -> list[str]:
    """The pre-flight's argv (the binary excluded): a tool-less, one-turn, ``--safe-mode``
    print-mode call with a JSON result, as the Claude Code provider builds one."""
    argv = [
        "-p",
        SAFE_MODE,
        "--output-format",
        "json",
        "--restricted",
        "--tools",
        "",
        "--max-turns",
        "1",
        "--no-session-persistence",
    ]
    if model:
        argv += ["--model", model]
    return argv


def one_line(text: str, limit: int = LINE_LIMIT) -> str:
    """``text`` on one line, its whitespace collapsed, at most ``limit`` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def is_sign_in_failure(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in SIGN_IN_MARKERS)


def _clause(reason: str) -> str:
    return one_line(reason).rstrip(" .") or "no reason given"


def sign_in_required_line(reason: str) -> str:
    """The one line a sign-in refusal prints, and records as its outcome's ``reason``."""
    return f'{SIGN_IN_REQUIRED_PREFIX} {_clause(reason)}. Run "claude auth login", then rerun.'


def preflight_failed_line(reason: str) -> str:
    """The one line any other pre-flight failure prints."""
    return f"{PREFLIGHT_FAILED_PREFIX} {_clause(reason)}; nothing was run."


def _result_object(res: ClaudeRunResult) -> dict[str, Any] | None:
    try:
        data = json.loads(res.stdout.strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _error_detail(data: dict[str, Any]) -> str:
    errors = data.get("errors")
    listed = ", ".join(str(e) for e in errors) if isinstance(errors, list) else ""
    return one_line(str(data.get("result") or data.get("error") or listed or ""))


def check_sign_in(
    runner: Runner,
    *,
    model: str | None = None,
    timeout_s: float = PREFLIGHT_TIMEOUT_S,
) -> str | None:
    """Make the one pre-flight call through ``runner`` and judge its answer.

    - Raises :class:`SignInRequired` when the answer names a sign-in failure
      (:data:`SIGN_IN_MARKERS`), with the CLI's own words as the reason.
    - Raises :class:`PreflightFailed` on any other failure: the runner's timeout or a binary
      that cannot start (it raises a ``HarnessError`` for both), an error result, or output
      that is not a JSON result.
    - Returns a warning to print when the call succeeded but ``claude`` wrote to stderr (the
      signed-in probe wrote nothing there), else None.
    """
    try:
        res = runner(preflight_argv(model), cwd=None, stdin=PREFLIGHT_PROMPT, timeout_s=timeout_s)
    except HarnessError as exc:
        raise PreflightFailed(one_line(str(exc)) or type(exc).__name__) from exc
    data = _result_object(res)
    if data is None:
        said = one_line(res.stderr or res.stdout)
        if said and is_sign_in_failure(said):
            raise SignInRequired(said)
        raise PreflightFailed(
            f"claude -p exited {res.code} with no JSON result" + (f": {said}" if said else "")
        )
    if data.get("is_error") or res.code != 0:
        detail = _error_detail(data)
        if detail and is_sign_in_failure(detail):
            raise SignInRequired(detail)
        subtype = str(data.get("subtype") or "-")
        raise PreflightFailed(
            f"claude -p exited {res.code}: {detail or 'no message'} (subtype={subtype})"
        )
    warning = one_line(res.stderr)
    return warning or None
