"""Issue #103: SCIO's credential pre-flight refuses a live command with one line,
``scio: sign-in required: <reason>. Run "claude auth login", then rerun.`` (exit 2, before
any work). The supervisor reads that line as ``external_auth``, whatever reason ``claude``
gave, and the tick escalates it at once and never starts a repair.

The line is written out here, not imported: the supervisor and SCIO share text, never code
(``tests/unit/test_preflight.py`` holds the two sides together through a real outcome file).
"""

from __future__ import annotations

import pytest

from recovery import machine
from recovery.classify import classify as real_classify  # bound before `world` patches it
from recovery.model import Evidence, FailureClass, IncidentState
from tests.recovery.test_machine import (  # noqa: F401 (fixtures register by import)
    World,
    world,
)

LINE = 'scio: sign-in required: {reason}. Run "claude auth login", then rerun.'


def _refusal(line: str) -> Evidence:
    """What the supervisor reads from a refused run: SCIO's outcome file and its stderr."""
    outcome = {
        "version": 1,
        "command": "build",
        "status": "refused",
        "exit_code": 2,
        "thread": None,
        "exit_reason": None,
        "reason": line,
        "resume": None,
        "exception": None,
    }
    return Evidence(exit_code=2, stderr_tail=f"{line}\n", outcome=outcome, last_ledger_line=line)


@pytest.mark.parametrize(
    "reason",
    [
        "Not logged in · Please run /login",  # what the probe printed signed out
        "the stored credential was rejected",  # no other auth marker: the prefix decides
    ],
)
def test_a_preflight_refusal_is_external_auth_and_never_repaired(
    world: World,  # noqa: F811
    reason: str,
) -> None:
    """#103: "The recovery supervisor classifies the pre-flight's refusal as
    `external_auth`. It escalates with 'sign in again' and never starts a repair." """
    w = world
    found = real_classify(_refusal(LINE.format(reason=reason)))
    assert found.failure_class is FailureClass.EXTERNAL_AUTH
    # every pre-flight refusal is one signature, so repeats gather in one incident
    assert found.signature == real_classify(_refusal(LINE.format(reason="x"))).signature
    iid = w.open("run-1", found)
    machine.tick(w.deps)  # one tick: snapshot, issue, escalation
    inc = w.get(iid)
    assert inc.state is IncidentState.ESCALATED
    assert inc.attempts == 0 and w.n("repair") == 0 and w.n("replay") == 0
    assert "Sign in again (`claude auth login`)" in inc.escalation
    assert inc.issue_number is not None
    assert "needs-human" in w.gh.issues[inc.issue_number]["labels"]


def test_a_preflight_that_failed_for_another_reason_is_not_external_auth() -> None:
    """A pre-flight timeout is waited out like any ``claude -p`` timeout; a ``claude`` that
    cannot start matches no class. Neither is a sign-in failure."""
    timed_out = (
        "scio: pre-flight check failed: claude -p timed out after 60s; its process tree was "
        "killed; nothing was run."
    )
    missing = "scio: pre-flight check failed: cannot start claude: not found; nothing was run."
    assert real_classify(_refusal(timed_out)).failure_class is FailureClass.EXTERNAL_TRANSIENT
    assert real_classify(_refusal(missing)).failure_class is FailureClass.UNKNOWN
