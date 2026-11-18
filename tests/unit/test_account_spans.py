"""Issue #102: a turn accounted outside ``Harness._call`` emits its span (I-122), and every
``invoke_agent`` span carries the thread id (I-123).

The span tests drive ``Harness.account`` and ``Harness.complete`` only, never the span helper
by name, so on the old code each one fails on its own assertion: no span, or no thread id.
"""

from __future__ import annotations

import ast
import inspect
import logging

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scio.harness import telemetry as T
from scio.harness.core import Harness
from scio.harness.providers.claude_code import PROVIDER_NAME
from scio.harness.types import Message, Usage

USAGE = Usage(input_tokens=1200, output_tokens=340, cache_read_tokens=5000, cache_write_tokens=80)


def _account(harness: Harness, *, latency_ms: int = 4200, thread: str = "t1") -> float:
    return harness.account(
        role="coder",
        model=harness.config.role("coder").model,
        provider=PROVIDER_NAME,
        usage=USAGE,
        latency_ms=latency_ms,
        request_id="session-abc",
        tags={"thread": thread, "step": "S2"},
    )


def test_an_accounted_turn_emits_an_invoke_agent_span(
    harness: Harness, spans: InMemorySpanExporter
) -> None:
    model = harness.config.role("coder").model
    usd = _account(harness)
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["invoke_agent coder"]
    attrs = dict(finished[0].attributes or {})
    assert attrs[T.OPERATION] == "invoke_agent"
    assert attrs[T.AGENT_NAME] == "coder"
    assert attrs[T.PROVIDER] == PROVIDER_NAME
    assert attrs[T.REQ_MODEL] == model and attrs[T.RES_MODEL] == model
    assert attrs[T.RES_ID] == "session-abc"
    assert attrs[T.IN_TOKENS] == 1200 and attrs[T.OUT_TOKENS] == 340
    assert attrs[T.CACHE_READ_TOKENS] == 5000 and attrs[T.CACHE_WRITE_TOKENS] == 80
    assert usd > 0 and attrs[T.SCIO_USD] == pytest.approx(usd)
    assert attrs[T.SCIO_ATTEMPTS] == 1
    assert attrs["scio.tag.step"] == "S2"
    # the ledger and the budget are recorded as before
    assert len(harness.ledger.entries) == 1 and harness.budget.snapshot().calls == 1


def test_invoke_agent_spans_carry_the_thread_id(
    harness: Harness, spans: InMemorySpanExporter
) -> None:
    req = harness.build_request(
        "gate", system="s", messages=[Message.user("u")], tags={"thread": "t1"}
    )
    harness.complete(req)
    _account(harness, thread="t1")
    finished = spans.get_finished_spans()
    for span in finished:
        attrs = dict(span.attributes or {})
        assert attrs.get("scio.thread_id") == "t1", span.name
        assert attrs[T.SCIO_THREAD_ID] == attrs["scio.tag.thread"] == "t1"
    assert sorted(s.name for s in finished) == ["invoke_agent coder", "invoke_agent gate"]


def test_a_call_without_a_thread_tag_sets_no_thread_id(
    harness: Harness, spans: InMemorySpanExporter
) -> None:
    harness.complete(harness.build_request("gate", system="s", messages=[Message.user("u")]))
    attrs = dict(spans.get_finished_spans()[0].attributes or {})
    assert "scio.thread_id" not in attrs


def test_every_telemetry_attribute_constant_is_set_by_a_span_helper() -> None:
    """Guardrail 125: an attribute name declared in ``telemetry.py`` and set by no function
    is a promise no trace keeps (I-123)."""
    tree = ast.parse(inspect.getsource(T))
    declared = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value.startswith(("gen_ai.", "scio."))
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    used = {
        n.id
        for fn in tree.body
        if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Name)
    }
    assert "SCIO_THREAD_ID" in declared
    assert declared - used == set()


def test_an_accounted_span_starts_latency_ms_before_it_ends(
    harness: Harness, spans: InMemorySpanExporter
) -> None:
    _account(harness, latency_ms=4200)
    span = spans.get_finished_spans()[0]
    assert span.start_time is not None and span.end_time is not None
    assert span.end_time - span.start_time == 4200 * 1_000_000


def test_a_tracing_failure_never_fails_the_accounted_turn(
    harness: Harness,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_tracer() -> object:
        raise RuntimeError("exporter down")

    monkeypatch.setattr(T, "tracer", broken_tracer)
    with caplog.at_level(logging.WARNING, logger=T.__name__):
        usd = _account(harness)
    assert usd > 0
    assert len(harness.ledger.entries) == 1 and harness.budget.snapshot().calls == 1
    assert spans.get_finished_spans() == ()
    assert "no span for the coder turn" in caplog.text
