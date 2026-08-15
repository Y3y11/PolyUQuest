"""W3C trace propagation across HTTP and durable queue boundaries.

Only fixed operation names and allow-listed scalar attributes are emitted. Business
content and dynamic identifiers must remain in the existing protected ledgers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agent_rag.config import settings

_TRACEPARENT = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
SAFE_TRACE_ATTRIBUTES = frozenset(
    {
        "attempt",
        "status",
        "stage",
        "route.mode",
        "route.source",
        "route.confidence",
        "cache.hit",
        "iteration",
        "evidence.count",
        "frontier.count",
        "candidate.count",
        "relevant_block.count",
        "webpage.count",
        "block.count",
        "link.count",
        "token.input",
        "token.output",
        "queue.wait_ms",
        "error.type",
    }
)


def validate_traceparent(value: str | None) -> str:
    """Return a canonical W3C version-00 header or an empty string."""
    if not isinstance(value, str):
        return ""
    selected = (value or "").strip().lower()
    match = _TRACEPARENT.fullmatch(selected)
    if match is None or match.group(1) == "0" * 32 or match.group(2) == "0" * 16:
        return ""
    return selected


def trace_id_from_traceparent(value: str | None) -> str | None:
    selected = validate_traceparent(value)
    return selected.split("-")[1] if selected else None


def _safe_attributes(values: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key not in SAFE_TRACE_ATTRIBUTES:
            continue
        if isinstance(value, bool | int | float) or isinstance(value, str) and len(value) <= 64:
            safe[key] = value
    return safe


@dataclass(frozen=True)
class ActiveTrace:
    traceparent: str = ""
    trace_id: str | None = None


class TraceRuntime:
    def __init__(
        self,
        *,
        mode: Literal["disabled", "propagate", "otlp"] = "disabled",
        service_name: str = "polyuquest",
        sample_ratio: float = 1.0,
        exporter: SpanExporter | None = None,
    ):
        self.mode = mode
        self._propagator = TraceContextTextMapPropagator()
        self._provider: TracerProvider | None = None
        if mode == "disabled":
            self._tracer = trace.get_tracer(__name__)
            return
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
            sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
        )
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
        self._provider = provider
        self._tracer = provider.get_tracer("polyuquest.runtime")

    @classmethod
    def from_settings(cls) -> TraceRuntime:
        exporter: SpanExporter | None = None
        if settings.otel_tracing_mode == "otlp":
            # The official exporter reads the standard OTEL_* variables itself,
            # including common endpoint path handling and secret headers.
            exporter = OTLPSpanExporter()
        return cls(
            mode=settings.otel_tracing_mode,
            service_name=settings.otel_service_name,
            sample_ratio=settings.otel_traces_sampler_arg,
            exporter=exporter,
        )

    def _context_from_carrier(self, traceparent: str | None) -> Context:
        selected = validate_traceparent(traceparent)
        if not selected:
            return Context()
        return self._propagator.extract({"traceparent": selected})

    def current_traceparent(self) -> str:
        if self.mode == "disabled":
            return ""
        carrier: dict[str, str] = {}
        self._propagator.inject(carrier)
        return validate_traceparent(carrier.get("traceparent"))

    @contextmanager
    def span(
        self,
        name: str,
        *,
        traceparent: str | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[ActiveTrace]:
        if self.mode == "disabled":
            yield ActiveTrace()
            return
        parent = self._context_from_carrier(traceparent)
        span = self._tracer.start_span(
            name,
            context=parent,
            kind=kind,
            attributes=_safe_attributes(attributes),
        )
        token = context.attach(trace.set_span_in_context(span, parent))
        try:
            carrier = self.current_traceparent()
            yield ActiveTrace(carrier, trace_id_from_traceparent(carrier))
        except BaseException as exc:
            span.set_attribute("error.type", exc.__class__.__name__[:64])
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            context.detach(token)
            span.end()

    def completed_span(
        self,
        name: str,
        *,
        duration_ms: int,
        status: str,
        attributes: Mapping[str, Any] | None = None,
        error_type: str = "",
    ) -> None:
        if self.mode == "disabled":
            return
        ended = time.time_ns()
        started = max(0, ended - max(0, duration_ms) * 1_000_000)
        safe = _safe_attributes(attributes)
        safe["status"] = status[:64]
        if error_type:
            safe["error.type"] = error_type[:64]
        span = self._tracer.start_span(name, start_time=started, attributes=safe)
        if status == "failed":
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=ended)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()


trace_runtime = TraceRuntime.from_settings()
