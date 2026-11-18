"""Issue #103: a live ``scio new`` or ``scio build`` on the Claude Code backend checks its
sign-in with one tool-less ``claude -p --safe-mode`` call before it writes anything.

Every ``claude`` here is an injected runner fed with what the real binary printed in the
probe (Claude Code 2.1.327, identifiers redacted): signed out, exit 1 and an error result;
signed in, exit 0 and nothing on stderr. No test runs a live ``claude``."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from recovery.classify import classify
from recovery.evidence import read_evidence
from recovery.model import FailureClass, RunRecord, RunStatus
from scio.cli import main
from scio.harness.failures import FatalError, RetryableError
from scio.harness.providers.claude_code import TIMEOUT_MESSAGE, ClaudeRunResult
from scio.settings import Settings

PROMPT = "A CLI todo app with tags and JSON export"

SIGNED_OUT = ClaudeRunResult(
    1,
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": None,
            "duration_ms": 53,
            "duration_api_ms": 0,
            "num_turns": 1,
            "result": "Not logged in · Please run /login",
            "stop_reason": "stop_sequence",
            "session_id": "<redacted>",
            "total_cost_usd": 0,
            "terminal_reason": "api_error",
            "modelUsage": {},
        }
    ),
    "",
)
"""``claude -p`` with an empty ``CLAUDE_CONFIG_DIR``, as the probe printed it."""

SIGNED_IN = ClaudeRunResult(
    0,
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "api_error_status": None,
            "duration_ms": 1525,
            "duration_api_ms": 1263,
            "num_turns": 1,
            "result": "OK",
            "stop_reason": "end_turn",
            "session_id": "<redacted>",
            "total_cost_usd": 0.0142098,
            "terminal_reason": "completed",
            "credential_expires_at": "2026-12-13T15:57:11.343Z",
        }
    ),
    "",
)
"""``claude -p`` signed in on the owner's Max login, as the probe printed it."""

STOP = ClaudeRunResult(
    0, json.dumps({"type": "result", "is_error": True, "result": "the test ends the run"}), ""
)
"""An error result that is no sign-in failure: the first real model call ends the command."""

SIGN_IN_LINE = (
    'scio: sign-in required: Not logged in · Please run /login. Run "claude auth login", '
    "then rerun."
)

Answer = ClaudeRunResult | BaseException


@dataclass
class Claude:
    """An injected ``claude``: each call is recorded and gets the next answer."""

    answers: list[Answer]
    calls: list[list[str]] = field(default_factory=list)
    stdins: list[str | None] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any = None,
        stdin: str | None = None,
        timeout_s: float = 600.0,
        env: Any = None,
    ) -> ClaudeRunResult:
        self.calls.append(list(argv))
        self.stdins.append(stdin)
        self.timeouts.append(timeout_s)
        assert self.answers, f"an unexpected claude call: {argv}"
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _install(monkeypatch: pytest.MonkeyPatch, claude: Callable[..., ClaudeRunResult]) -> None:
    """Every ``claude`` SCIO starts (the pre-flight, the provider, the worker) is ``claude``."""
    monkeypatch.setattr(
        "scio.harness.providers.claude_code.claude_runner", lambda binary="claude": claude
    )


def _never(*_: Any, **__: Any) -> ClaudeRunResult:
    raise AssertionError("the Fake provider ran claude")


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCIO_WORKER_BACKEND", raising=False)
    monkeypatch.delenv("SCIO_FAKE_SCRIPT", raising=False)
    monkeypatch.setenv("SCIO_PRODUCT_SANDBOX_BACKEND", "local")  # no Docker probe
    monkeypatch.setattr("scio.product.pr_ci.CI_WAIT_S", 0.0)
    monkeypatch.setattr("scio.product.pr_ci.CI_POLL_S", 0.0)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A product bootstrapped on the Fake provider, which never runs ``claude``; then the
    environment switches to the Claude Code backend."""
    monkeypatch.setenv("SCIO_CHECKPOINT_DSN", f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}")
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    monkeypatch.setenv("SCIO_FAKE_SCRIPT", "1")
    _install(monkeypatch, _never)
    target = str(tmp_path / "product")
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-cli"]) == 0
    assert main(["new", "--thread", "new-cli", "--decision", "answers"]) == 0
    assert main(["new", "--thread", "new-cli", "--decision", "approve"]) == 0
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
    monkeypatch.delenv("SCIO_FAKE_SCRIPT")
    return target


def _tree(root: Path, skip: str = "out") -> dict[str, tuple[int, int]]:
    """Every file and directory under ``root`` but ``skip``: its size and write time."""
    found: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts[0] == skip:
            continue
        st = path.stat()
        found[rel.as_posix()] = (st.st_size if path.is_file() else -1, st.st_mtime_ns)
    return found


def _is_preflight(argv: list[str]) -> bool:
    """A tool-less, one-turn, ``--safe-mode`` print-mode call, as the probe ran it: on the
    account's default model, with no system prompt file (nothing is written for it)."""
    pairs = list(itertools.pairwise(argv))
    return (
        argv[0] == "-p"
        and "--safe-mode" in argv
        and ("--tools", "") in pairs
        and ("--max-turns", "1") in pairs
        and ("--output-format", "json") in pairs
        and "--no-session-persistence" in argv
        and "--model" not in argv
        and "--system-prompt-file" not in argv
    )


# ---------- invariant 1: the pre-flight comes before the first model call ----------


@pytest.mark.parametrize("command", ["new", "build"])
def test_a_live_build_runs_the_preflight_before_any_model_call(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#103: "One tool-less `claude -p --safe-mode` call before the first real model call of
    a live command." The second call is a role's real call (its system prompt file), and the
    test ends the command there."""
    if command == "build":
        argv = ["build", "--product", _bootstrap(tmp_path, monkeypatch)]
    else:
        monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
        argv = ["new", "--prompt", PROMPT, "--dir", str(tmp_path / "product")]
    claude = Claude([SIGNED_IN, STOP])
    _install(monkeypatch, claude)
    assert main(argv) == 1  # the harness stop on the test's error result
    assert len(claude.calls) == 2
    assert _is_preflight(claude.calls[0]), claude.calls[0]
    assert claude.stdins[0] == "Reply with OK." and claude.timeouts[0] == 60.0
    assert "--system-prompt-file" in claude.calls[1] and not _is_preflight(claude.calls[1])


def test_a_warning_on_a_signed_in_preflight_is_printed_and_the_command_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
    warned = ClaudeRunResult(0, SIGNED_IN.stdout, "a notice from claude\n  on two lines\n")
    claude = Claude([warned, STOP])
    _install(monkeypatch, claude)
    assert main(["new", "--prompt", PROMPT, "--dir", str(tmp_path / "product")]) == 1
    err = capsys.readouterr().err
    assert "scio: warning: a notice from claude on two lines\n" in err
    assert len(claude.calls) == 2  # it went on to the interviewer


# ---------- invariant 2: a sign-in failure is exit 2, one line, and no state ----------


@pytest.mark.parametrize("command", ["new", "new --thread", "build", "build --decision"])
def test_a_signin_failure_exits_2_with_the_instruction_and_no_state(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#103: "A sign-in failure ends the command with exit 2 and the sign-in instruction,
    and leaves no thread state behind." Nothing under the workspace changes: no checkpoint
    store, no ``.scio`` file, no product directory, no thread. A ``new --thread`` resume is
    refused before it reads the checkpoint store, which opening would create."""
    if command.startswith("new"):
        monkeypatch.delenv("SCIO_CHECKPOINT_DSN", raising=False)  # the default, under the cwd
        monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
        argv = ["new", "--prompt", PROMPT, "--dir", str(tmp_path / "product")]
        if command == "new --thread":
            argv = ["new", "--thread", "new-x", "--decision", "answers"]
    else:
        argv = ["build", "--product", _bootstrap(tmp_path, monkeypatch)]
        if command == "build --decision":
            argv += ["--decision", "approve"]
    outcome = tmp_path / "out" / "outcome.json"
    outcome.parent.mkdir()
    before = _tree(tmp_path)
    claude = Claude([SIGNED_OUT])
    _install(monkeypatch, claude)
    capsys.readouterr()
    assert main([*argv, "--outcome-file", str(outcome)]) == 2
    out, err = capsys.readouterr()
    assert err.splitlines() == [SIGN_IN_LINE] and out == ""
    assert len(claude.calls) == 1 and _is_preflight(claude.calls[0])
    assert _tree(tmp_path) == before
    if command.startswith("new"):
        assert before == {}  # not even the default checkpoint store under the cwd
    data = json.loads(outcome.read_text(encoding="utf-8"))
    assert (data["status"], data["exit_code"], data["thread"], data["reason"]) == (
        "refused",
        2,
        None,
        SIGN_IN_LINE,
    )


def test_a_preflight_that_cannot_run_claude_exits_2_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``claude`` that cannot start (or a timeout) is ``PreflightFailed``: exit 2, one
    line naming it, never the sign-in line."""
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
    missing = FatalError("cannot start claude: [WinError 2] The system cannot find the file")
    claude = Claude([missing])
    _install(monkeypatch, claude)
    assert main(["new", "--prompt", PROMPT, "--dir", str(tmp_path / "product")]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "scio: pre-flight check failed: cannot start claude: [WinError 2] The system cannot "
        "find the file; nothing was run."
    ]
    assert list(tmp_path.iterdir()) == []


# ---------- invariant 3: the Fake provider (and an API provider) skip it ----------


def test_the_fake_provider_skips_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#103: "The Fake provider skips the check, and a test covers both paths with an
    injected runner." ``scio new`` and ``scio build`` on the Fake provider never start
    ``claude`` (the runner fails the test if they do); the other path is the tests above.

    The build goes past the pre-flight's place to the ``--decision`` check after it, which
    refuses: on the Claude Code backend the same command makes the pre-flight call
    (``test_a_signin_failure_exits_2_with_the_instruction_and_no_state[build --decision]``).
    A whole milestone on the Fake provider takes 40 seconds and shows nothing more."""
    from scio.harness.preflight import preflight_applies

    target = _bootstrap(tmp_path, monkeypatch)  # three `scio new` calls, the Fake provider
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    capsys.readouterr()
    assert main(["build", "--product", target, "--decision", "approve"]) == 2
    assert "has no checkpoint" in capsys.readouterr().err
    # an API provider skips it too; only the claude_code provider runs it
    assert not preflight_applies(Settings(provider_override="fake"))
    assert not preflight_applies(Settings(provider_override="fake", worker_backend="claude_code"))
    assert not preflight_applies(Settings(provider_override=None))
    assert not preflight_applies(Settings(provider_override="anthropic"))
    assert preflight_applies(Settings(provider_override="claude_code"))


# ---------- check_sign_in on its own ----------


def test_the_preflight_call_is_tool_less_safe_mode_and_writes_nothing() -> None:
    from scio.harness.preflight import check_sign_in, preflight_argv

    assert preflight_argv("m") == [
        "-p",
        "--safe-mode",
        "--output-format",
        "json",
        "--restricted",
        "--tools",
        "",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--model",
        "m",
    ]
    assert "--model" not in preflight_argv()  # the probed call: the default model
    claude = Claude([SIGNED_IN])
    assert check_sign_in(claude) is None
    assert claude.calls == [preflight_argv()] and claude.timeouts == [60.0]
    assert claude.stdins == ["Reply with OK."]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (SIGNED_OUT, "Not logged in · Please run /login"),
        # a stdout decoded with errors="replace" may have lost the middle dot
        (
            ClaudeRunResult(1, SIGNED_OUT.stdout.replace("\\u00b7", "\\ufffd"), ""),
            "Not logged in � Please run /login",
        ),
        (ClaudeRunResult(1, "", "Error: Not logged in\n"), "Error: Not logged in"),
        (
            ClaudeRunResult(
                1,
                json.dumps({"is_error": True, "result": "OAuth token has expired"}),
                "",
            ),
            "OAuth token has expired",
        ),
    ],
)
def test_a_signed_out_answer_is_sign_in_required_with_claudes_words(
    answer: ClaudeRunResult, reason: str
) -> None:
    from scio.harness.preflight import SignInRequired, check_sign_in

    with pytest.raises(SignInRequired) as caught:
        check_sign_in(Claude([answer]))
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        (RetryableError(f"{TIMEOUT_MESSAGE} after 60s; its process tree was killed"), "timed out"),
        (FatalError("cannot start claude: [Errno 2] No such file"), "cannot start claude"),
        (
            ClaudeRunResult(0, json.dumps({"is_error": True, "result": "Overloaded"}), ""),
            "Overloaded",
        ),
        (ClaudeRunResult(0, "not json", ""), "no JSON result: not json"),
        (ClaudeRunResult(1, "", ""), "exited 1 with no JSON result"),
    ],
)
def test_any_other_failure_is_preflight_failed(answer: Answer, names: str) -> None:
    from scio.harness.preflight import PreflightFailed, check_sign_in

    with pytest.raises(PreflightFailed) as caught:
        check_sign_in(Claude([answer]))
    assert names in caught.value.reason


def test_the_lines_are_one_line_and_only_a_sign_in_failure_has_the_sign_in_prefix() -> None:
    from scio.harness.preflight import preflight_failed_line, sign_in_required_line

    assert sign_in_required_line("Not logged in · Please run /login") == SIGN_IN_LINE
    assert sign_in_required_line("OAuth token\nhas expired.  ") == (
        'scio: sign-in required: OAuth token has expired. Run "claude auth login", then rerun.'
    )
    failed = preflight_failed_line("claude -p timed out after 60s")
    assert failed == (
        "scio: pre-flight check failed: claude -p timed out after 60s; nothing was run."
    )
    assert "sign-in required" not in failed


# ---------- the supervisor reads each refusal SCIO writes ----------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (SIGNED_OUT, FailureClass.EXTERNAL_AUTH),
        (
            RetryableError(f"{TIMEOUT_MESSAGE} after 60s; its process tree was killed"),
            FailureClass.EXTERNAL_TRANSIENT,
        ),
        (FatalError("cannot start claude: [WinError 2] not found"), FailureClass.UNKNOWN),
    ],
)
def test_the_supervisor_reads_the_outcome_scio_writes_for_each_preflight_refusal(
    answer: Answer,
    expected: FailureClass,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The contract with ``recovery/classify.py`` (the two sides share text, never code): a
    sign-in refusal is ``external_auth``; a timeout or a missing ``claude`` never is."""
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")
    _install(monkeypatch, Claude([answer]))
    run_dir = tmp_path / "out"
    run_dir.mkdir()
    argv = ["new", "--prompt", PROMPT, "--dir", str(tmp_path / "product")]
    assert main([*argv, "--outcome-file", str(run_dir / "outcome.json")]) == 2
    out, err = capsys.readouterr()
    (run_dir / "stdout.log").write_text(out, encoding="utf-8", newline="\n")
    (run_dir / "stderr.log").write_text(err, encoding="utf-8", newline="\n")
    run = RunRecord(
        run_id="run-1",
        workspace=str(tmp_path),
        launcher=["sh", "scio.sh"],
        argv=argv,
        env={},
        started_at=0.0,
        status=RunStatus.EXITED,
        exit_code=2,
        run_dir=str(run_dir),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        outcome_path=str(run_dir / "outcome.json"),
    )
    found = classify(read_evidence(run))
    assert found.failure_class is expected, found
    if expected is FailureClass.EXTERNAL_AUTH:
        assert "claude auth login" in found.summary
