"""OpenTelemetry GenAI spans.

Attribute names follow the OTel GenAI semantic conventions (``gen_ai.*``), which are
still in Development status; the version is pinned in :data:`SEMCONV_VERSION` and any
rename is a one-line change here. Prompt and completion text are recorded only when
``SCIO_OTEL_CAPTURE_CONTENT=1``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, StatusCode

from scio.harness.types import CompletionRequest, CompletionResponse, Usage

log = logging.getLogger(__name__)

SEMCONV_VERSION = "1.37.0"

# gen_ai.* attribute keys (pinned, see SEMCONV_VERSION)
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQ_MODEL = "gen_ai.request.model"
RES_MODEL = "gen_ai.response.model"
RES_ID = "gen_ai.response.id"
IN_TOKENS = "gen_ai.usage.input_tokens"
OUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
FINISH_REASONS = "gen_ai.response.finish_reasons"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
# scio.* keys
SCIO_USD = "scio.usd"
SCIO_ATTEMPTS = "scio.attempts"
SCIO_THREAD_ID = "scio.thread_id"
"""The thread id, from a call's ``thread`` tag (I-123); the tag is also ``scio.tag.thread``."""

_TRACER_NAME = "scio"
_configured_provider: TracerProvider | None = None


def configure_tracing(
    exporter: Literal["none", "console", "otlp"] | SpanExporter = "none",
    *,
    service_name: str = "scio",
    simple: bool = False,
) -> TracerProvider:
    """Install a tracer provider and attach an exporter.

    OpenTelemetry allows one global provider per process. The first call creates it;
    later calls attach their exporter to the existing provider (so tests can add an
    in-memory exporter without fighting the global).
    """
    global _configured_provider
    if _configured_provider is None:
        _configured_provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        trace.set_tracer_provider(_configured_provider)
    provider = _configured_provider
    span_exporter: SpanExporter | None
    if isinstance(exporter, SpanExporter):
        span_exporter = exporter
    elif exporter == "console":
        span_exporter = ConsoleSpanExporter()
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()  # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
    else:
        span_exporter = None
    if span_exporter is not None:
        processor = (
            SimpleSpanProcessor(span_exporter) if simple else BatchSpanProcessor(span_exporter)
        )
        provider.add_span_processor(processor)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME, SEMCONV_VERSION)


def _set_agent_attributes(
    span: Span, *, role: str, provider: str, model: str, tags: Mapping[str, str]
) -> None:
    """The attributes every ``invoke_agent`` span carries, through the harness or not."""
    span.set_attribute(OPERATION, "invoke_agent")
    span.set_attribute(AGENT_NAME, role)
    span.set_attribute(PROVIDER, provider)
    span.set_attribute(REQ_MODEL, model)
    for k, v in tags.items():
        span.set_attribute(f"scio.tag.{k}", v)
    if "thread" in tags:
        span.set_attribute(SCIO_THREAD_ID, tags["thread"])


def _set_usage_attributes(span: Span, usage: Usage, *, usd: float, attempts: int) -> None:
    span.set_attribute(IN_TOKENS, usage.input_tokens)
    span.set_attribute(OUT_TOKENS, usage.output_tokens)
    span.set_attribute(CACHE_READ_TOKENS, usage.cache_read_tokens)
    span.set_attribute(CACHE_WRITE_TOKENS, usage.cache_write_tokens)
    span.set_attribute(SCIO_USD, usd)
    span.set_attribute(SCIO_ATTEMPTS, attempts)


@contextmanager
def invoke_agent_span(
    request: CompletionRequest, *, provider: str, capture_content: bool = False
) -> Iterator[Span]:
    """Span for one model call made on behalf of a SCIO role."""
    role = request.role or "model"
    with tracer().start_as_current_span(f"invoke_agent {role}") as span:
        _set_agent_attributes(
            span, role=role, provider=provider, model=request.model, tags=request.tags
        )
        if capture_content:
            span.set_attribute(SYSTEM_INSTRUCTIONS, request.system)
            span.set_attribute(
                INPUT_MESSAGES,
                json.dumps([m.model_dump(mode="json") for m in request.messages]),
            )
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise


def record_response(
    span: Span,
    response: CompletionResponse,
    *,
    usd: float,
    attempts: int,
    capture_content: bool = False,
) -> None:
    span.set_attribute(RES_MODEL, response.model)
    if response.request_id:
        span.set_attribute(RES_ID, response.request_id)
    _set_usage_attributes(span, response.usage, usd=usd, attempts=attempts)
    span.set_attribute(FINISH_REASONS, [response.stop_reason.value])
    if capture_content:
        span.set_attribute(
            OUTPUT_MESSAGES, json.dumps(response.as_assistant_message().model_dump(mode="json"))
        )


def account_span(
    *,
    role: str,
    model: str,
    provider: str,
    usage: Usage,
    usd: float,
    latency_ms: int = 0,
    request_id: str | None = None,
    tags: Mapping[str, str] | None = None,
) -> None:
    """One ``invoke_agent`` span for a finished turn made outside ``Harness._call``: a Claude
    Code worker turn (ADR 0012, I-122). The turn is over when it is accounted, so the span
    starts ``latency_ms`` before now and ends now. It carries what an API turn's span does,
    except the finish reason, which such a turn does not report. With no tracer configured
    it is OpenTelemetry's no-op. A failure here is logged and never raised: the ledger and
    the budget are recorded before this runs, and tracing must not fail the turn."""
    try:
        end = time.time_ns()
        span = tracer().start_span(
            f"invoke_agent {role}", start_time=end - max(latency_ms, 0) * 1_000_000
        )
        try:
            _set_agent_attributes(span, role=role, provider=provider, model=model, tags=tags or {})
            span.set_attribute(RES_MODEL, model)
            if request_id:
                span.set_attribute(RES_ID, request_id)
            _set_usage_attributes(span, usage, usd=usd, attempts=1)
        finally:
            span.end(end_time=end)
    except Exception:
        log.warning("no span for the %s turn %s", role, request_id or "", exc_info=True)


@contextmanager
def execute_tool_span(tool_name: str, **attrs: Any) -> Iterator[Span]:
    """Span for one tool execution (used by the ACI layer from M2)."""
    with tracer().start_as_current_span(f"execute_tool {tool_name}") as span:
        span.set_attribute(OPERATION, "execute_tool")
        span.set_attribute(TOOL_NAME, tool_name)
        for k, v in attrs.items():
            span.set_attribute(f"scio.{k}", v)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise
