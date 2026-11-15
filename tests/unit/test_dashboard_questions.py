"""The dashboard's open questions and its answer box (ADR 0020 decision 6, issue #141 part 5).

A parked feature's design question is listed with its feature, attempt, issue and age, and the
answer box sends the owner's answer: an ``answer:`` comment on the feature's issue when the
build has a GitHub repository, and the workspace's answer file in every case, so a failed post
loses nothing and says why. Both speak the formats of :mod:`scio.product.questions`. The
endpoint is the dashboard's one write: it needs the per-launch token, and it never writes a
checkpoint (ADR 0017: the build stays the only writer of its own state)."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

# ruff exempts pytest.importorskip from E402: the imports below need the dashboard extra
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from scio.dashboard import app as dashboard_app
from scio.dashboard.app import create_app
from scio.dashboard.data import CheckpointDataSource, workspace
from scio.graph.checkpointer import serializer
from scio.harness.github.client import RunResult
from scio.product import questions
from scio.state.models import Budget, FeatureRun, ProductBuildState, Tier
from tests.dashboard_support import FixtureDataSource

BASE = "http://127.0.0.1"
WS_URL = "ws://127.0.0.1/ws"
TOKEN = "launch-token-for-the-tests"
ROOT = "ws"
BUILD = "build-calc-m1"
REPO = "owner/calc"
ISSUE = 12
ASKED = {
    "P1-a1": "2026-11-14T09:00:00+00:00",
    "P1-a2": "2026-11-14T10:00:00+00:00",
    "P3-a1": "2026-11-14T08:00:00+00:00",
}
"""Each parked attempt's own thread's latest checkpoint: the question's age."""
GH_401 = (
    "HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
    "Try authenticating with:  gh auth login\n"
)
"""A failed ``gh`` call's stderr: the answer's ``gh_error`` is its first line."""


@dataclass
class FakeGh:
    """The injected ``gh`` runner: records each call; never a live ``gh``."""

    code: int = 0
    stderr: str = ""
    raises: Exception | None = None
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, stdin: str | None = None) -> RunResult:
        self.calls.append((list(argv), stdin))
        if self.raises is not None:
            raise self.raises
        return RunResult(self.code, "", self.stderr)


def _run(feature: str, attempt: int, status: str, question: str | None = None) -> FeatureRun:
    parked = status == "parked"
    return FeatureRun(
        feature_id=feature,
        thread_id=f"{BUILD}-{feature.lower()}-a{attempt}",
        attempt=attempt,
        tier=Tier.T1,
        status=status,  # type: ignore[arg-type]
        issue_number=ISSUE if feature == "P1" else ISSUE + int(feature[1:]),
        milestone="M1",
        question=question,
        question_id=questions.question_id(feature, attempt) if parked else None,
    )


RUNS = [
    _run("P1", 1, "parked", "Which parser?"),  # superseded by attempt 2: not listed
    _run("P2", 1, "passed"),
    _run("P1", 2, "parked", "Left or right associative <b>powers</b>?"),
    _run("P3", 1, "parked", "Float or decimal?"),
    _run("P4", 1, "parked", "Which units?"),
    _run("P4", 2, "passed"),  # answered and built: no longer an open question
]


def _put(conn: sqlite3.Connection, thread_id: str, channels: dict[str, Any], ts: str) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts
    checkpoint["channel_values"] = channels
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    SqliteSaver(conn, serde=serializer()).put(config, checkpoint, {}, {})  # type: ignore[arg-type]


@dataclass(frozen=True)
class World:
    source: CheckpointDataSource
    db: Path
    path: Path


def _world(tmp_path: Path, *, gh_repo: str | None) -> World:
    """One root holding a build thread with parked feature runs, and their own threads."""
    path = tmp_path / ROOT
    db = path / ".scio" / "checkpoints.sqlite"
    db.parent.mkdir(parents=True)
    state = ProductBuildState(
        thread_id=BUILD,
        product_dir=str(path / "calc"),
        gh_repo=gh_repo,
        phase="feature",
        issues={"P1": ISSUE, "P2": 13, "P3": 14, "P4": 15},
        feature_runs=RUNS,
        budgets=Budget(tokens_max=1_000, usd_max=1.0, advisor_calls_max=0),
    )
    with closing(sqlite3.connect(db)) as conn:
        _put(conn, BUILD, state.model_dump(mode="json"), "2026-11-14T11:00:00+00:00")
        for run in RUNS:
            qid = questions.question_id(run.feature_id, run.attempt)
            ts = ASKED.get(qid, "2026-11-14T07:00:00+00:00")
            _put(conn, run.thread_id, {"thread_id": run.thread_id}, ts)
        conn.commit()
    ws = workspace(path, name=ROOT)
    source = CheckpointDataSource([ws], recovery_store=tmp_path / "no-recovery-store.sqlite")
    return World(source, db, Path(ws.path))


def _client(world: World, gh: FakeGh | None = None) -> TestClient:
    app = create_app(world.source, answer_token=TOKEN, gh=gh or FakeGh())
    return TestClient(app, base_url=BASE)


def _answer(c: TestClient, qid: str, text: str, token: str | None = TOKEN) -> Any:
    headers = {"X-SCIO-Answer-Token": token} if token is not None else {}
    body = {"root": ROOT, "thread": BUILD, "question_id": qid, "text": text}
    return c.post("/api/answer", json=body, headers=headers)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- invariant 1: the dashboard shows the open questions (ADR 0020) ----


def test_a_parked_feature_is_listed_as_an_open_question(tmp_path: Path) -> None:
    world = _world(tmp_path, gh_repo=REPO)
    questions.write_answer_file(world.path, "P3-a1", "Decimal.")
    with _client(world) as c:
        listed = c.get("/api/questions")
        with c.websocket_connect(WS_URL) as ws:
            hello = ws.receive_json()
    assert listed.status_code == 200
    rows = listed.json()
    # the latest run of each feature, still parked; the longest-waiting first
    assert [(q["feature_id"], q["attempt"], q["question_id"], q["answered"]) for q in rows] == [
        ("P3", 1, "P3-a1", True),
        ("P1", 2, "P1-a2", False),
    ]
    p1 = rows[1]
    assert p1["question"] == "Left or right associative <b>powers</b>?"  # text, as asked
    assert (p1["root"], p1["thread_id"], p1["milestone"]) == (ROOT, BUILD, "M1")
    assert (p1["feature_thread"], p1["feature_present"]) == (f"{BUILD}-p1-a2", True)
    assert (p1["issue_number"], p1["gh_repo"]) == (ISSUE, REPO)
    assert p1["issue_url"] == f"https://github.com/{REPO}/issues/{ISSUE}"
    assert datetime.fromisoformat(p1["asked_at"]) == datetime(2026, 11, 14, 10, tzinfo=UTC)
    assert p1["answer_file"] == str(questions.answer_path(world.path, "P1-a2"))
    assert hello["questions"] is True  # the page reads /api/questions only from such a server


def test_a_source_without_questions_lists_none_and_says_so_in_the_hello() -> None:
    with TestClient(create_app(FixtureDataSource(), answer_token=TOKEN), base_url=BASE) as c:
        assert c.get("/api/questions").json() == []
        with c.websocket_connect(WS_URL) as ws:
            assert ws.receive_json()["questions"] is False
        assert _answer(c, "P1-a1", "anything").status_code == 404


# ---- invariant 2: and can answer them ----


def test_an_answer_without_a_repo_writes_the_answer_file(tmp_path: Path) -> None:
    world, gh = _world(tmp_path, gh_repo=None), FakeGh()
    with _client(world, gh) as c:
        before = {q["question_id"]: q for q in c.get("/api/questions").json()}
        assert before["P1-a2"]["issue_url"] is None  # offline: the issue number is a file index
        resp = _answer(c, "P1-a2", "  Right associative, like maths.  ")
        after = {q["question_id"]: q["answered"] for q in c.get("/api/questions").json()}
    assert resp.status_code == 200, resp.text
    path = questions.answer_path(world.path, "P1-a2")
    assert resp.json() == {
        "question_id": "P1-a2",
        "posted": False,
        "issue_number": None,
        "issue_url": None,
        "saved": str(path),
        "gh_error": None,
    }
    assert questions.read_answer_file(world.path, "P1-a2") == "Right associative, like maths."
    assert gh.calls == []
    assert after == {"P1-a2": True, "P3-a1": False}


def test_an_answer_with_a_repo_posts_an_answer_comment_on_the_issue(tmp_path: Path) -> None:
    world, gh = _world(tmp_path, gh_repo=REPO), FakeGh()
    with _client(world, gh) as c:
        resp = _answer(c, "P1-a2", "Right associative.")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "question_id": "P1-a2",
        "posted": True,
        "issue_number": ISSUE,
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}",
        "saved": str(questions.answer_path(world.path, "P1-a2")),
        "gh_error": None,
    }
    # the runner prepends `gh`: gh issue comment 12 --repo owner/calc --body-file -
    assert [argv for argv, _ in gh.calls] == [
        ["issue", "comment", str(ISSUE), "--repo", REPO, "--body-file", "-"]
    ]
    body = gh.calls[0][1]
    assert body is not None and body.startswith(questions.ANSWER_PREFIX)
    # the build reads it back with the contract's parser, after the question's marker comment
    asked = questions.format_question(questions.Question("P1", 2, "Left or right?"))
    assert questions.parse_answer([asked, body], "P1-a2") == "Right associative."


def test_an_answer_posted_to_the_issue_is_also_recorded_locally(tmp_path: Path) -> None:
    """The answer file too: the question reads answered after a reload (the dashboard reads no
    issue comments), and the build finds the answer even before it reads the issue."""
    world, gh = _world(tmp_path, gh_repo=REPO), FakeGh()
    with _client(world, gh) as c:
        resp = _answer(c, "P1-a2", "  Right associative.  ")
        assert resp.json()["posted"] is True and len(gh.calls) == 1
        reloaded = {q["question_id"]: q["answered"] for q in c.get("/api/questions").json()}
    assert resp.json()["saved"] == str(questions.answer_path(world.path, "P1-a2"))
    assert questions.read_answer_file(world.path, "P1-a2") == "Right associative."
    assert reloaded == {"P1-a2": True, "P3-a1": False}


@pytest.mark.parametrize(
    ("gh", "gh_error"),
    [
        (FakeGh(code=1, stderr=GH_401), GH_401.splitlines()[0]),
        (
            FakeGh(code=dashboard_app.GH_TIMED_OUT, stderr="gh timed out after 30 s"),
            "gh timed out after 30 s",
        ),
        (FakeGh(code=4, stderr=""), "gh exited 4"),
        (FakeGh(raises=RuntimeError("runner broke")), "RuntimeError: runner broke"),
    ],
    ids=["http-401", "timeout", "no-stderr", "runner-raised"],
)
def test_a_failed_issue_post_still_saves_the_answer_and_says_so(
    tmp_path: Path, gh: FakeGh, gh_error: str
) -> None:
    """A failed or timed-out post never loses the answer, and the failure shows: 200, the
    answer file written, and the first line of why the post failed."""
    world = _world(tmp_path, gh_repo=REPO)
    with _client(world, gh) as c:
        resp = _answer(c, "P1-a2", "Right associative.")
        reloaded = {q["question_id"]: q["answered"] for q in c.get("/api/questions").json()}
    assert resp.status_code == 200, resp.text
    path = questions.answer_path(world.path, "P1-a2")
    assert resp.json() == {
        "question_id": "P1-a2",
        "posted": False,
        "issue_number": ISSUE,
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}",
        "saved": str(path),
        "gh_error": gh_error,
    }
    assert len(gh.calls) == 1
    assert questions.read_answer_file(world.path, "P1-a2") == "Right associative."
    assert reloaded["P1-a2"] is True


def test_the_gh_call_is_bounded_at_30_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default runner passes the timeout to ``run_tree``, which kills the ``gh`` process
    tree it timed out (guardrail 74); a timeout and a missing ``gh`` are failed runs, never a
    raise. No live ``gh``."""
    seen: list[dict[str, Any]] = []

    def timing_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": argv, **kwargs})
        raise subprocess.TimeoutExpired(argv, kwargs["timeout_s"])

    monkeypatch.setattr(dashboard_app, "run_tree", timing_out)
    done = dashboard_app.gh_runner(["issue", "comment", "12"], stdin="answer: x")
    assert seen[0]["argv"] == ["gh", "issue", "comment", "12"]
    assert seen[0]["timeout_s"] == dashboard_app.GH_TIMEOUT_S == 30
    assert seen[0]["input"] == "answer: x"
    assert (done.code, done.stderr) == (dashboard_app.GH_TIMED_OUT, "gh timed out after 30 s")

    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(dashboard_app, "run_tree", missing)
    assert dashboard_app.gh_runner(["issue"]).code == 127


# ---- invariant 3: the dashboard still never writes a checkpoint ----


def test_the_answer_endpoint_never_writes_a_checkpoint(tmp_path: Path) -> None:
    for gh_repo in (None, REPO):
        world = _world(tmp_path / str(gh_repo).replace("/", "-"), gh_repo=gh_repo)
        before, versions = _sha(world.db), world.source.versions(ROOT)
        with _client(world) as c:
            assert c.get("/api/questions").status_code == 200
            assert _answer(c, "P1-a2", "Right associative.").status_code == 200
            assert _answer(c, "P1-a1", "too late").status_code == 404
        assert _sha(world.db) == before, gh_repo
        assert world.source.versions(ROOT) == versions


# ---- invariant 4: the endpoint is protected ----


def test_the_answer_endpoint_needs_the_launch_token(tmp_path: Path) -> None:
    world, gh = _world(tmp_path, gh_repo=None), FakeGh()
    answer_file = questions.answer_path(world.path, "P1-a2")
    with _client(world, gh) as c:
        assert _answer(c, "P1-a2", "no token", token=None).status_code == 403
        assert _answer(c, "P1-a2", "wrong token", token="guess").status_code == 403
        assert _answer(c, "P1-a2", "", token=None).status_code == 403  # the token comes first
        for empty in ("", "   \n"):
            assert _answer(c, "P1-a2", empty).status_code == 400
        not_json = c.post("/api/answer", content=b"{", headers={"X-SCIO-Answer-Token": TOKEN})
        assert not_json.status_code == 400
        assert _answer(c, "P1-a1", "superseded").status_code == 404  # not the latest run
        assert _answer(c, "P9-a1", "unknown").status_code == 404
        foreign = c.post(
            "/api/answer",
            json={"root": ROOT, "thread": BUILD, "question_id": "P1-a2", "text": "x"},
            headers={"X-SCIO-Answer-Token": TOKEN, "Origin": "http://evil.example"},
        )
        assert foreign.status_code == 403  # another site's page, even holding the token
        assert not answer_file.exists() and gh.calls == []
        assert _answer(c, "P1-a2", "Right.").status_code == 200
    assert questions.read_answer_file(world.path, "P1-a2") == "Right."


def test_the_page_carries_the_launch_token_and_each_launch_makes_its_own(tmp_path: Path) -> None:
    world = _world(tmp_path, gh_repo=None)
    pages = []
    for _ in range(2):
        with TestClient(create_app(world.source), base_url=BASE) as c:
            page = c.get("/")
            assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
            pages.append(page.text)
    tokens = [p.split('name="scio-answer-token" content="', 1)[1].split('"', 1)[0] for p in pages]
    assert all(len(t) >= 32 for t in tokens) and tokens[0] != tokens[1]
    with TestClient(create_app(world.source, answer_token=TOKEN), base_url=BASE) as c:
        assert f'name="scio-answer-token" content="{TOKEN}"' in c.get("/").text
