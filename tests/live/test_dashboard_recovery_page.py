"""The dashboard's recovery view in a real browser (issue #102), moved here from
tests/unit/test_dashboard_recovery.py so that every Chromium test runs in CI's tests/live shard,
away from the unit tests' workers (I-224). The fixtures and helpers stay in the unit module and
are imported from it."""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import closing
from typing import Any

from recovery.model import IncidentState
from scio.dashboard.app import create_app
from tests.browser_support import requires_browser
from tests.unit.test_dashboard_recovery import (  # noqa: F401 (fixtures register by import)
    BUILD,
    FEATURE,
    PID,
    ROOT,
    World,
    _incident,
    _no_real_store,
    world,
)


class _Server:
    """The real app under uvicorn in a thread, with a bounded shutdown (guardrail 28)."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", timeout_graceful_shutdown=3
        )
        self.server = uvicorn.Server(config)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:
            self.error = exc

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), self.error
            time.sleep(0.02)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        assert not self.thread.is_alive(), "uvicorn did not stop"
        assert self.error is None, self.error


@requires_browser
def test_the_page_shows_the_incidents_and_a_thread_that_stopped(world: World) -> None:  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    world.answers[PID] = True
    world.run("run-1", incident="inc-1")
    with closing(world.store()) as store:
        store.save_incident(
            _incident(
                "inc-1",
                world,
                run_ids=["run-1"],
                issue_number=81,
                pr_number=82,
                escalation="needs the owner",
            )
        )
        store.add_event("inc-1", "issue opened")
        store.save_incident(
            _incident(
                "inc-esc",
                world,
                run_ids=["run-1"],
                state=IncidentState.ESCALATED,
                escalation="repair failed twice",
                issue_number=91,
                pr_number=92,
                updated_at=time.time() - 60,
            )
        )
    source = world.source()
    console: list[str] = []
    errors: list[str] = []
    with _Server(create_app(source, poll_interval_s=0.05)) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
            page.goto(base + "/", wait_until="domcontentloaded")
            incident = page.locator('tr.incident[data-incident="inc-1"]')
            expect(incident).to_contain_text("issue_open")
            expect(incident).to_contain_text("issue #81 · PR #82")
            link = incident.locator("a.incident-thread")
            expect(link).to_have_attribute("href", f"#/thread/{ROOT}/{BUILD}")
            expect(page.locator('tr[data-sub-for-incident="inc-1"] .escalation')).to_contain_text(
                "needs the owner"
            )
            # the escalated incident comes first, as "needs you", with its reason and link
            groups = page.locator(".incidents tbody[data-group]")
            assert groups.evaluate_all("gs => gs.map(g => g.dataset.group)") == [
                "needs-you",
                "open",
            ]
            needs_you = groups.first
            expect(needs_you.locator(".group-label")).to_contain_text("needs you")
            escalated = needs_you.locator('tr.incident[data-incident="inc-esc"]')
            expect(escalated).to_contain_text("issue #91 · PR #92")
            expect(escalated.locator(".badge.i-escalating")).to_have_text("escalated")
            expect(escalated.locator("a.incident-thread")).to_have_attribute(
                "href", f"#/thread/{ROOT}/{BUILD}"
            )
            expect(needs_you.locator(".escalation")).to_contain_text("repair failed twice")
            build_row = page.locator(f'tr.row[data-thread="{ROOT}/{BUILD}"]')
            expect(build_row.locator(".badge")).to_have_text("in progress")
            page.evaluate("window.__kept = true")

            world.answers[PID] = False  # the build is killed: the page follows, no reload
            expect(build_row.locator(".badge.s-stopped")).to_have_text("stopped")
            expect(build_row.locator(".badge")).to_have_attribute(
                "title", re.compile(f"PID {PID}, is gone")
            )
            sub = page.locator(f'tr[data-sub-for="{ROOT}/{FEATURE}"] .stopped-box')
            expect(sub).to_contain_text(f"PID {PID}, is gone")
            assert page.evaluate("window.__kept === true")
            width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            assert width[0] <= width[1], f"the list scrolls sideways: {width}"

            link.click()
            head = page.locator(f'.head[data-thread="{ROOT}/{BUILD}"]')
            expect(head.locator(".stopped-box")).to_contain_text("supervised run run-1")
            expect(head.locator('.incident-box[data-incident="inc-1"]')).to_contain_text(
                "issue #81"
            )
            # the escalated one first on the thread too
            expect(head.locator(".incident-box").first).to_have_attribute(
                "data-incident", "inc-esc"
            )
            expect(head.locator(".incident-box").first).to_contain_text("repair failed twice")
            assert page.locator("button, form, input, select, textarea").count() == 0
        finally:
            browser.close()
    assert errors == []
    assert all("Failed to load resource" in c for c in console), console  # the aborted CDN


@requires_browser
def test_the_page_says_when_there_is_no_supervisor_store(world: World) -> None:  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    with _Server(create_app(world.source(), poll_interval_s=0.05)) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.goto(base + "/", wait_until="domcontentloaded")
            section = page.locator('.recovery-section[data-recovery="absent"]')
            expect(section).to_contain_text("No recovery supervisor store.")
            expect(section).to_contain_text(str(world.store_path))
        finally:
            browser.close()
