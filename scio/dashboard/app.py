"""The dashboard's server (issue #68, ADR 0017): :func:`create_app` serves a
:class:`~scio.dashboard.contract.DataSource` as JSON, the page in ``static/``, and ``/ws``, which
pushes a :class:`~scio.dashboard.contract.ChangeEvent` whenever a poll of
:meth:`~scio.dashboard.contract.DataSource.versions` finds a thread new, changed or gone. Every
route is a read but one, ``POST /api/answer`` (ADR 0020; ``test_every_route_is_a_read`` guards
that it stays the only one). One root that cannot be read never hides the others: the threads
list skips it and logs why. The server binds 127.0.0.1, yet any web page the user visits can
reach it: ``TrustedHostMiddleware`` refuses a foreign ``Host`` (DNS rebinding), ``/ws`` accepts
only the page this server served (``Origin`` equal to ``Host``, so not another local dev server
either), and every response forbids framing. Needs the ``dashboard`` extra; the CLI imports
this module lazily.

A source that is also a :class:`~scio.dashboard.contract.RecoverySource` (the checkpoint data
source is) serves the recovery supervisor's view on ``/api/recovery`` (issue #102). Its
``/ws`` hello carries ``recovery``, and so does each later event that found the supervisor's
store changed, so the page asks for that view only from a server that has it.

A source that is also a :class:`~scio.dashboard.contract.QuestionSource` (the checkpoint data
source is) lists the parked features' design questions on ``/api/questions`` and takes the
owner's answers on ``POST /api/answer`` (ADR 0020 decision 6, issue #141 part 5). The answer
goes to the feature's issue as an ``answer:`` comment through the injected ``gh`` runner when
the build has a GitHub repository (bounded at :data:`GH_TIMEOUT_S`), and to the workspace's
answer file in every case (:mod:`scio.product.questions`), so a failed post loses nothing and
says why; never into a checkpoint, so the build stays the only writer of its own state. The
endpoint takes a token the app makes at each launch, which only the page this server served
carries (its ``scio-answer-token`` meta tag), and refuses another site's ``Origin``."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import secrets
import subprocess
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from scio.dashboard.contract import (
    AnswerRequest,
    AnswerResult,
    ChangeEvent,
    DataSource,
    OpenQuestion,
    QuestionSource,
    RecoverySource,
    RecoveryView,
    ThreadRef,
    ThreadSummary,
    ThreadVersion,
    UnknownQuestion,
    UnknownRoot,
    UnknownThread,
)
from scio.harness.github.client import Runner, RunResult
from scio.proctree import run_tree
from scio.product import questions

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
"""The page: ``index.html``, ``app.js``, ``style.css``."""

ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<netloc>[^/?#@\s]+)", re.IGNORECASE)
"""An ``Origin`` header as a browser serializes it: a scheme and ``host[:port]``, nothing else
(no user info, no path). ``null`` and every other scheme do not match."""

DEFAULT_PORTS = {"http": "80", "https": "443"}

PAGE_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
"""Pinned for the page's files: on Windows ``mimetypes`` reads the registry, which can map
``.js`` to ``text/plain``, and a browser drops a stylesheet that is not ``text/css``."""

FRAME_HEADERS = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}
"""On every HTTP response: no site may frame the page (clickjacking). A ``<meta>`` CSP cannot
set ``frame-ancestors``; the browser enforces this header and the page's own meta policy both,
and this one names no script or style directive, so it loosens nothing."""

POLICY_VIOLATION = 1008
"""The WebSocket close code for a refused ``Origin`` (RFC 6455 section 7.4.1)."""

ANSWER_TOKEN_HEADER = "X-SCIO-Answer-Token"
"""The header ``POST /api/answer`` reads the launch token from. A custom header also makes a
browser preflight a cross-site request, which this server never allows."""

TOKEN_META = '<meta name="scio-answer-token" content="">'
"""The tag in ``index.html`` the page reads the launch token from; ``/`` fills it in."""

TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]{16,}")
"""A launch token: ``secrets.token_urlsafe``'s alphabet, so it sits in an HTML attribute as is."""

GH_TIMEOUT_S = 30
"""How long the answer comment's ``gh`` may take; then it is killed and the answer is kept in
the workspace's answer file only."""

GH_TIMED_OUT = 124
"""The exit code :func:`gh_runner` reports for a ``gh`` it killed (``timeout``'s convention)."""


def gh_runner(argv: Sequence[str], *, stdin: str | None = None) -> RunResult:
    """``gh`` with ``argv``, ``stdin`` as its input, bounded at :data:`GH_TIMEOUT_S`: a
    :class:`~scio.harness.github.client.Runner`. ``GhRunner`` has no timeout, and a hung ``gh``
    would hold the answer request open. A timeout or a missing ``gh`` is a failed run, never a
    raise. :func:`scio.proctree.run_tree` runs it, whose timeout kills the whole process tree
    (guardrail 74, ``test_no_subprocess_timeout_outside_run_tree``)."""
    try:
        proc = run_tree(["gh", *argv], timeout_s=GH_TIMEOUT_S, input=stdin)
    except subprocess.TimeoutExpired:
        return RunResult(GH_TIMED_OUT, "", f"gh timed out after {GH_TIMEOUT_S} s")
    except OSError as exc:
        return RunResult(127, "", f"cannot start gh: {exc}")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def first_line(text: str, fallback: str) -> str:
    """The first non-blank line of ``text``, stripped; ``fallback`` when there is none."""
    return next((line.strip() for line in text.splitlines() if line.strip()), fallback)


Snapshot = dict[str, dict[str, ThreadVersion]]
"""Root name -> thread id -> its version, as of one poll."""


def _host_port(netloc: str, scheme: str) -> str:
    """``host[:port]`` lowercased, the scheme's default port dropped, so ``Origin`` and
    ``Host`` compare equal however each spells the port."""
    netloc = netloc.lower()
    default = DEFAULT_PORTS.get(scheme, "")
    return netloc.removesuffix(f":{default}") if default else netloc


def is_same_origin(origin: str, host: str | None) -> bool:
    """``origin`` is ``http(s)://`` plus the request's own ``Host`` header, port included: the
    page this server served. Another port on localhost (a dev server), another host, ``null``
    or a missing ``Host`` is a foreign page."""
    match = ORIGIN.fullmatch(origin)
    if match is None or host is None:
        return False
    scheme = match["scheme"].lower()
    return _host_port(match["netloc"], scheme) == _host_port(host, scheme)


def newest_first(rows: Sequence[ThreadSummary]) -> list[ThreadSummary]:
    """Newest ``updated_at`` first, a thread without one last; ties keep their order."""

    def key(t: ThreadSummary) -> tuple[bool, float]:
        return (t.updated_at is None, -t.updated_at.timestamp() if t.updated_at else 0.0)

    return sorted(rows, key=key)


def longest_waiting_first(rows: Sequence[OpenQuestion]) -> list[OpenQuestion]:
    """The oldest ``asked_at`` first, a question of unknown age last; ties keep their order."""

    def key(q: OpenQuestion) -> tuple[bool, float]:
        return (q.asked_at is None, q.asked_at.timestamp() if q.asked_at else 0.0)

    return sorted(rows, key=key)


def diff_versions(previous: Snapshot, current: Snapshot) -> ChangeEvent:
    """A thread in ``current`` whose version is new or differs goes to ``changed``; a thread of
    ``previous`` that ``current`` no longer has goes to ``removed``."""
    changed = [
        ThreadRef(root=root, thread_id=tid)
        for root, threads in current.items()
        for tid, version in threads.items()
        if previous.get(root, {}).get(tid) != version
    ]
    removed = [
        ThreadRef(root=root, thread_id=tid)
        for root, threads in previous.items()
        for tid in threads
        if tid not in current.get(root, {})
    ]
    return ChangeEvent(changed=changed, removed=removed, at=datetime.now(UTC))


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class FailureStreaks:
    """Logs a root that cannot be read without filling the terminal: the first failure of a
    streak at warning, the repeats at debug, the recovery at info. Thread-safe, since the sync
    endpoints run in FastAPI's threadpool and share one."""

    def __init__(self, what: str) -> None:
        self._what = what
        self._failing: set[str] = set()
        self._lock = threading.Lock()

    def failed(self, root: str, exc: BaseException) -> None:
        with self._lock:
            first = root not in self._failing
            self._failing.add(root)
        level = logging.WARNING if first else logging.DEBUG
        log.log(level, "dashboard: %s root %s failed, skipped: %s", self._what, root, _reason(exc))

    def succeeded(self, root: str) -> None:
        with self._lock:
            if root not in self._failing:
                return
            self._failing.discard(root)
        log.info("dashboard: %s root %s works again", self._what, root)


class FrameDenyMiddleware:
    """Adds :data:`FRAME_HEADERS` to every HTTP response, the 404s, 405s and the Host check's
    400s included (it is added last, so it wraps the other middleware)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in FRAME_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _snapshot(source: DataSource, previous: Snapshot, streaks: FailureStreaks) -> Snapshot:
    """Every root's versions. A root whose read raises keeps its previous versions this round,
    so its threads are neither removed nor changed."""
    try:
        names = [w.name for w in await asyncio.to_thread(source.roots)]
    except Exception as exc:
        log.warning("dashboard: listing the roots failed: %s", _reason(exc))
        return previous
    current: Snapshot = {}
    for name in names:
        try:
            current[name] = await asyncio.to_thread(source.versions, name)
        except Exception as exc:
            streaks.failed(name, exc)
            if name in previous:
                current[name] = previous[name]
            continue
        streaks.succeeded(name)
    return current


RECOVERY_STORE = "the recovery supervisor's store"
"""The name a failing :meth:`RecoverySource.recovery_version` is logged under."""


async def _store_version(
    recovery: RecoverySource | None, previous: str, streaks: FailureStreaks
) -> str:
    """The supervisor store's version; ``previous`` when there is no recovery source or the
    read raises (logged once per streak), so a failing read never reads as a change."""
    if recovery is None:
        return previous
    try:
        version = await asyncio.to_thread(recovery.recovery_version)
    except Exception as exc:
        streaks.failed(RECOVERY_STORE, exc)
        return previous
    streaks.succeeded(RECOVERY_STORE)
    return version


async def _push_changes(
    websocket: WebSocket,
    source: DataSource,
    poll_interval_s: float,
    recovery: RecoverySource | None = None,
    questions_on: bool = False,
) -> None:
    """Accept, take the baseline, send the hello, then poll every ``poll_interval_s`` and push
    a :class:`ChangeEvent` only when something changed. The baseline comes before the hello: a
    change that lands after the page got the hello is always pushed. One receive reads the
    socket the whole time and is never cancelled while the loop runs, so a closed tab ends the
    loop at once, however the server's receive handles a cancel (I-202). What the client
    sends is ignored. With a
    ``recovery`` source the hello says so, and a poll that finds the supervisor's store
    changed pushes ``recovery``. ``questions_on``: the source lists open questions, which the
    hello says."""
    await websocket.accept()
    streaks = FailureStreaks("polling")  # per connection: each socket logs its own streaks
    previous = await _snapshot(source, {}, streaks)
    store = await _store_version(recovery, "", streaks)
    hello = ChangeEvent(at=datetime.now(UTC), recovery=recovery is not None, questions=questions_on)
    await websocket.send_text(hello.model_dump_json())
    # One read of the socket lives as long as the connection and is never cancelled while it
    # does: a receive cancelled at each poll can lose the disconnect handed to it in the same
    # loop iteration (anyio's memory stream does), and the loop would then poll forever (I-202).
    leaving = asyncio.ensure_future(_until_disconnect(websocket))
    try:
        while True:
            done, _ = await asyncio.wait({leaving}, timeout=poll_interval_s)
            if done:
                leaving.result()  # a receive that failed raises here
                return
            current = await _snapshot(source, previous, streaks)
            event = diff_versions(previous, current)
            previous = current
            now = await _store_version(recovery, store, streaks)
            event.recovery, store = now != store, now
            if event.changed or event.removed or event.recovery:
                await websocket.send_text(event.model_dump_json())
    finally:
        leaving.cancel()  # the loop is over: losing a message no longer matters
        await asyncio.wait({leaving})
        if not leaving.cancelled():
            leaving.exception()  # retrieved, so asyncio logs no "never retrieved"


async def _until_disconnect(websocket: WebSocket) -> None:
    """Read the socket until the client leaves; what the client sends is ignored."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


def create_app(
    source: DataSource,
    *,
    poll_interval_s: float = 1.0,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
    answer_token: str | None = None,
    gh: Runner = gh_runner,
) -> FastAPI:
    """The dashboard over ``source``. ``static_dir`` (default :data:`STATIC_DIR`) is read per
    request, so a missing directory never stops the app from starting: ``/`` answers 404 then.
    ``allowed_hosts`` are the ``Host`` headers served; any other gets 400. ``answer_token``:
    the launch token ``POST /api/answer`` requires, by default a new
    ``secrets.token_urlsafe(32)`` per app. ``gh`` runs ``gh`` for the answer comment (its
    argv without ``gh``, the body on stdin), by default :func:`gh_runner`, bounded at
    :data:`GH_TIMEOUT_S`; the tests inject one."""
    if poll_interval_s <= 0:
        raise ValueError(f"poll_interval_s must be positive, got {poll_interval_s}")
    token = answer_token if answer_token is not None else secrets.token_urlsafe(32)
    if not TOKEN_CHARS.fullmatch(token):
        raise ValueError("answer_token must be 16 or more of A-Z, a-z, 0-9, '_' and '-'")
    token_meta = TOKEN_META.replace('content=""', f'content="{token}"')
    static = static_dir if static_dir is not None else STATIC_DIR
    for suffix, media_type in PAGE_TYPES.items():
        mimetypes.add_type(media_type, suffix)  # FileResponse guesses with this table
    app = FastAPI(title="SCIO dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(FrameDenyMiddleware)  # added last: outermost, so the 400s carry it too
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")
    listing = FailureStreaks("listing the threads of")
    asking = FailureStreaks("listing the open questions of")
    recovery_source = source if isinstance(source, RecoverySource) else None
    question_source = source if isinstance(source, QuestionSource) else None

    # Plain `def` endpoints: the source does blocking sqlite I/O, and FastAPI runs sync
    # endpoints in its threadpool. They return JSONResponse, so FastAPI infers no
    # response_model and the contract's JSON (model_dump(mode="json")) goes out as is.

    @app.get("/")
    def index() -> HTMLResponse:
        """The page, its ``scio-answer-token`` meta tag filled with this launch's token. Not
        stored: a page from an earlier launch would carry a stale token."""
        page = static / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail=f"the dashboard page is missing: {page}")
        html = page.read_text(encoding="utf-8").replace(TOKEN_META, token_meta, 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/roots")
    def roots() -> JSONResponse:
        return JSONResponse([w.model_dump(mode="json") for w in source.roots()])

    @app.get("/api/threads")
    def threads(root: str | None = None) -> JSONResponse:
        """Every root's threads, or ``root``'s. A root that cannot be read (a file that is not
        a database, a foreign schema) is skipped and logged, so it never hides the others;
        asked for by name, it answers 503 with the reason."""
        names = [root] if root is not None else [w.name for w in source.roots()]
        rows: list[ThreadSummary] = []
        for name in names:
            try:
                rows.extend(source.list_threads(name))
            except UnknownRoot:
                raise HTTPException(status_code=404, detail=f"no root named {name!r}") from None
            except Exception as exc:
                listing.failed(name, exc)
                if root is not None:
                    raise HTTPException(
                        status_code=503, detail=f"root {name!r} cannot be read: {_reason(exc)}"
                    ) from None
                continue
            listing.succeeded(name)
        return JSONResponse([t.model_dump(mode="json") for t in newest_first(rows)])

    @app.get("/api/threads/{root}/{thread_id:path}")
    def thread(root: str, thread_id: str) -> JSONResponse:
        try:
            detail = source.thread(root, thread_id)
        except UnknownRoot:
            raise HTTPException(status_code=404, detail=f"no root named {root!r}") from None
        except UnknownThread:
            raise HTTPException(
                status_code=404, detail=f"root {root!r} has no thread {thread_id!r}"
            ) from None
        except Exception as exc:
            log.warning("dashboard: reading %s/%s failed: %s", root, thread_id, _reason(exc))
            raise HTTPException(
                status_code=503, detail=f"root {root!r} cannot be read: {_reason(exc)}"
            ) from None
        return JSONResponse(detail.model_dump(mode="json"))

    @app.get("/api/graphs")
    def graphs() -> JSONResponse:
        return JSONResponse([g.model_dump(mode="json") for g in source.graphs()])

    @app.get("/api/recovery")
    def recovery() -> JSONResponse:
        """The supervisor's view; an empty one (no store) from a source that reads none. A
        missing or unreadable store is part of the view, not an error status."""
        if recovery_source is None:
            return JSONResponse(RecoveryView().model_dump(mode="json"))
        try:
            view = recovery_source.recovery()
        except Exception as exc:
            log.warning("dashboard: reading the recovery view failed: %s", _reason(exc))
            raise HTTPException(
                status_code=503, detail=f"the recovery view cannot be read: {_reason(exc)}"
            ) from None
        return JSONResponse(view.model_dump(mode="json"))

    @app.get("/api/questions")
    def open_questions() -> JSONResponse:
        """Every root's open questions (ADR 0020), the longest-waiting first; none from a
        source that lists none. A root that cannot be read is skipped and logged."""
        if question_source is None:
            return JSONResponse([])
        rows: list[OpenQuestion] = []
        for name in [w.name for w in source.roots()]:
            try:
                rows.extend(question_source.questions(name))
            except Exception as exc:
                asking.failed(name, exc)
                continue
            asking.succeeded(name)
        return JSONResponse([q.model_dump(mode="json") for q in longest_waiting_first(rows)])

    def post(q: OpenQuestion, text: str) -> str | None:
        """The ``answer:`` comment on ``q``'s issue; None when it went through, else the
        first line of why not (a non-zero exit, a timeout, a runner that raised)."""
        argv = ["issue", "comment", str(q.issue_number), "--repo", str(q.gh_repo)]
        try:
            done = gh([*argv, "--body-file", "-"], stdin=f"{questions.ANSWER_PREFIX} {text}")
        except Exception as exc:
            return first_line(_reason(exc), "gh failed")
        if done.code == 0:
            return None
        return first_line(done.stderr, f"gh exited {done.code}")

    def deliver(req: AnswerRequest, text: str) -> AnswerResult:
        """Find the open question and send ``text`` where its build reads answers: an
        ``answer:`` comment on the feature's issue when the build has a repository, and the
        workspace's answer file in every case, so a failed post loses nothing. Reads the
        checkpoint DB read-only and never writes it (ADR 0017, ADR 0020)."""
        if question_source is None:
            raise HTTPException(status_code=404, detail="this dashboard lists no questions")
        try:
            q = question_source.question(req.root, req.thread, req.question_id)
        except (UnknownRoot, UnknownQuestion):
            raise HTTPException(
                status_code=404,
                detail=f"no open question {req.question_id!r} in {req.root}/{req.thread}",
            ) from None
        except Exception as exc:
            log.warning("dashboard: finding question %s failed: %s", req.question_id, _reason(exc))
            raise HTTPException(
                status_code=503, detail=f"root {req.root!r} cannot be read: {_reason(exc)}"
            ) from None
        on_issue = bool(q.gh_repo) and q.issue_number is not None
        ws = next((w for w in source.roots() if w.name == q.root), None)
        savable = ws is not None and q.answer_file is not None
        if not (on_issue or savable):
            raise HTTPException(
                status_code=409,
                detail=f"question id {q.question_id!r} cannot name an answer file and the "
                "build has no repository; answer at the build's stop instead",
            )
        gh_error = post(q, text) if on_issue else None
        if on_issue and gh_error is None:
            log.info("dashboard: answered %s on %s#%s", q.question_id, q.gh_repo, q.issue_number)
        elif on_issue:
            log.warning("dashboard: posting the answer to %s failed: %s", q.question_id, gh_error)
        saved: Path | None = None
        if ws is not None and savable:
            try:
                saved = questions.write_answer_file(Path(ws.path), q.question_id, text)
            except OSError as exc:
                if not on_issue or gh_error is not None:  # nowhere holds the answer: say so
                    raise HTTPException(
                        status_code=500,
                        detail=f"the answer was not kept: {_reason(exc)}"
                        + (f"; posting to the issue failed: {gh_error}" if gh_error else ""),
                    ) from None
                log.warning("dashboard: posted %s, not recorded locally: %s", q.question_id, exc)
            else:
                log.info("dashboard: answer to %s saved in %s", q.question_id, saved)
        elif gh_error is not None:  # the post failed and no file can hold the answer
            raise HTTPException(status_code=502, detail=f"posting to the issue failed: {gh_error}")
        return AnswerResult(
            question_id=q.question_id,
            posted=on_issue and gh_error is None,
            issue_number=q.issue_number if on_issue else None,
            issue_url=q.issue_url if on_issue else None,
            saved=str(saved) if saved is not None else None,
            gh_error=gh_error,
        )

    @app.post("/api/answer")
    async def answer(request: Request) -> JSONResponse:
        """The dashboard's one write (ADR 0020 decision 6). In this order: 403 for another
        site's page or without this launch's token, 400 for a body that is not an
        :class:`AnswerRequest` or an empty answer, 404 for no such open question. A failed
        post that the answer file still holds answers 200 with ``gh_error``; only an answer
        kept nowhere is an error (500 or 502)."""
        origin = request.headers.get("origin")
        if origin is not None and not is_same_origin(origin, request.headers.get("host")):
            log.info("dashboard: refused an answer from origin %r", origin)
            raise HTTPException(status_code=403, detail="answers come only from this page")
        given = request.headers.get(ANSWER_TOKEN_HEADER, "")
        if not secrets.compare_digest(given.encode(), token.encode()):
            raise HTTPException(
                status_code=403,
                detail="missing or wrong answer token: reload the page (a relaunch makes one)",
            )
        try:
            req = AnswerRequest.model_validate_json(await request.body())
        except ValidationError:
            raise HTTPException(
                status_code=400, detail="the body must be JSON {root, thread, question_id, text}"
            ) from None
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="an answer cannot be empty")
        result = await asyncio.to_thread(deliver, req, text)
        return JSONResponse(result.model_dump(mode="json"))

    @app.websocket("/ws")
    async def changes(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and not is_same_origin(origin, websocket.headers.get("host")):
            log.info("dashboard: refused a /ws connection from origin %r", origin)
            await websocket.close(code=POLICY_VIOLATION)
            return
        try:
            await _push_changes(
                websocket,
                source,
                poll_interval_s,
                recovery_source,
                questions_on=question_source is not None,
            )
        except (WebSocketDisconnect, WebSocketDisconnected):
            return  # the client went away mid-send: nothing to report

    return app
