"""The open questions and the answer box in a real browser (ADR 0020 decision 6, issue #141
part 5): the real app over a real checkpoint DB. The question shows as text, its count beside
the other attention items, a live redraw keeps a half-typed answer and its caret, and Send
carries the launch token, so the answer file is written and the question reads answered after a
reload. A failed issue post shows its reason, and the answer is saved all the same."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from scio.dashboard.app import create_app
from scio.product import questions
from tests.browser_support import requires_browser
from tests.live.test_dashboard_recovery_page import _Server
from tests.unit.test_dashboard_questions import ISSUE, REPO, FakeGh, _put, _world

DRAFT = "Right associative, "


@requires_browser
def test_the_answer_box_keeps_a_draft_through_a_redraw_and_sends_it(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    world = _world(tmp_path, gh_repo=None)
    console: list[str] = []
    errors: list[str] = []
    app = create_app(world.source, poll_interval_s=0.05)
    with _Server(app) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
            page.goto(base + "/", wait_until="domcontentloaded")
            p1 = page.locator('.question-card[data-question="P1-a2"]')
            # the question is text: its markup shows as written and makes no element
            expect(p1.locator(".q-text")).to_have_text("Left or right associative <b>powers</b>?")
            assert p1.locator(".q-text b").count() == 0
            expect(page.locator(".question-card")).to_have_count(2)
            expect(page.locator(".summary-line .q-chip")).to_contain_text("open questions 2")

            box = p1.locator("textarea.q-answer")
            box.click()
            box.press_sequentially(DRAFT)
            # a new checkpoint redraws the list while the answer is being typed
            page.evaluate("document.querySelector('.questions-section').dataset.old = '1'")
            with closing(sqlite3.connect(world.db)) as conn:
                _put(conn, "run-other", {"thread_id": "run-other"}, "2026-11-14T12:00:00+00:00")
                conn.commit()
            page.wait_for_selector(".questions-section:not([data-old])", timeout=10_000)
            box = page.locator('.question-card[data-question="P1-a2"] textarea.q-answer')
            expect(box).to_have_value(DRAFT)
            expect(box).to_be_focused()
            box.press_sequentially("like maths.")  # the caret stayed at the end
            expect(box).to_have_value(DRAFT + "like maths.")

            page.locator('.question-card[data-question="P1-a2"] button.q-send').click()
            p1 = page.locator('.question-card[data-question="P1-a2"]')
            saved = questions.answer_path(world.path, "P1-a2")
            expect(p1.locator(".q-state")).to_have_text(
                f"Saved to {saved}; waiting for the build's next pass", timeout=10_000
            )
            expect(page.locator(".summary-line .q-chip")).to_contain_text("open questions 1")
            assert questions.read_answer_file(world.path, "P1-a2") == DRAFT + "like maths."
            page.reload(wait_until="domcontentloaded")  # the file says answered, on any page
            expect(page.locator('.question-card[data-question="P1-a2"] .q-state')).to_have_text(
                "answered, waiting for the build's next pass", timeout=10_000
            )
            assert errors == []
            assert all("Failed to load resource" in c for c in console), console  # the CDN
            assert len(console) <= 2, console  # the aborted CDN per load only: no API failure
        finally:
            browser.close()


@requires_browser
def test_a_failed_issue_post_shows_and_the_answer_is_saved(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    world = _world(tmp_path, gh_repo=REPO)
    gh = FakeGh(code=1, stderr="HTTP 401: Bad credentials\nTry authenticating with: gh auth login")
    errors: list[str] = []
    with (
        _Server(create_app(world.source, poll_interval_s=0.05, gh=gh)) as base,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/", wait_until="domcontentloaded")
            p1 = page.locator('.question-card[data-question="P1-a2"]')
            expect(p1.locator(".q-where")).to_have_text(
                f"posts an answer: comment on issue #{ISSUE} and records it locally"
            )
            p1.locator("textarea.q-answer").fill("Right associative.")
            p1.locator("button.q-send").click()
            state = page.locator('.question-card[data-question="P1-a2"] .q-state.q-warn')
            expect(state).to_have_text(
                f"Saved locally; posting to issue #{ISSUE} failed: HTTP 401: Bad credentials; "
                "waiting for the build's next pass",
                timeout=10_000,
            )
            assert len(gh.calls) == 1
            assert questions.read_answer_file(world.path, "P1-a2") == "Right associative."
            assert errors == []
        finally:
            browser.close()
