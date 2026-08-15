"""Bounded Tempo query client and privacy-safe waterfall projection."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from agent_rag.config import settings
from agent_rag.tracing.runtime import SAFE_TRACE_ATTRIBUTES

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_SAFE_SERVICES = frozenset(
    {"polyuquest-api", "polyuquest-worker", "polyuquest-bff", "polyuquest-e2e"}
)
_SAFE_SPAN_NAMES = frozenset(
    {
        "agent.run.submit",
        "agent.run.execute",
        "agent.stage",
        "llm.chat",
        "knowledge.index.execute",
        "bff.request",
        "runtime.unknown",
    }
)

TraceScalar = bool | int | float | str


class TraceBackendDisabledError(RuntimeError):
    pass


class TraceNotFoundError(RuntimeError):
    pass


class TraceBackendUnavailableError(RuntimeError):
    pass


class TraceBackendResponseTooLargeError(RuntimeError):
    pass


class TraceBackendInvalidResponseError(RuntimeError):
    pass


class TraceSpanView(BaseModel):
    span_id: str
    parent_span_id: str = ""
    name: str
    service: str
    kind: Literal[
        "unspecified", "internal", "server", "client", "producer", "consumer"
    ]
    status: Literal["unset", "ok", "error"]
    start_offset_ms: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    attributes: dict[str, TraceScalar] = Field(default_factory=dict)


class TraceWaterfall(BaseModel):
    trace_id: str
    services: list[str]
    span_count: int = Field(ge=0)
    returned_span_count: int = Field(ge=0)
    truncated: bool
    duration_ms: float = Field(ge=0)
    spans: list[TraceSpanView]


def validate_trace_id(value: str) -> str:
    selected = value.strip().lower()
    if not _TRACE_ID.fullmatch(selected) or selected == "0" * 32:
        raise ValueError("trace_id must be 32 non-zero hexadecimal characters")
    return selected


def _normalize_binary_id(value: Any, size: int) -> str:
    if not isinstance(value, str):
        return ""
    selected = value.strip().lower()
    pattern = _TRACE_ID if size == 16 else _SPAN_ID
    if pattern.fullmatch(selected) and selected != "0" * (size * 2):
        return selected
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return ""
    if len(decoded) != size or decoded == b"\0" * size:
        return ""
    return decoded.hex()


def _scalar(value: Any) -> TraceScalar | None:
    if not isinstance(value, Mapping):
        return None
    if isinstance(value.get("boolValue"), bool):
        return bool(value["boolValue"])
    if isinstance(value.get("stringValue"), str):
        selected = value["stringValue"]
        return selected if len(selected) <= 64 else None
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError, OverflowError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _attributes(values: Any, *, allowlist: Iterable[str]) -> dict[str, TraceScalar]:
    allowed = frozenset(allowlist)
    result: dict[str, TraceScalar] = {}
    if not isinstance(values, list):
        return result
    for item in values:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key")
        if not isinstance(key, str) or key not in allowed:
            continue
        selected = _scalar(item.get("value"))
        if selected is not None:
            result[key] = selected
    return result


def _service(resource: Any) -> str:
    attributes = resource.get("attributes", []) if isinstance(resource, Mapping) else []
    values = _attributes(attributes, allowlist={"service.name"})
    selected = values.get("service.name")
    return selected if isinstance(selected, str) and selected in _SAFE_SERVICES else "unknown"


def _integer(value: Any) -> int | None:
    try:
        selected = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return selected if selected >= 0 else None


def _kind(value: Any) -> str:
    mapping = {
        0: "unspecified",
        1: "internal",
        2: "server",
        3: "client",
        4: "producer",
        5: "consumer",
        "SPAN_KIND_UNSPECIFIED": "unspecified",
        "SPAN_KIND_INTERNAL": "internal",
        "SPAN_KIND_SERVER": "server",
        "SPAN_KIND_CLIENT": "client",
        "SPAN_KIND_PRODUCER": "producer",
        "SPAN_KIND_CONSUMER": "consumer",
    }
    return mapping.get(value, "unspecified")


def _status(value: Any) -> str:
    code = value.get("code") if isinstance(value, Mapping) else None
    mapping = {
        0: "unset",
        1: "ok",
        2: "error",
        "STATUS_CODE_UNSET": "unset",
        "STATUS_CODE_OK": "ok",
        "STATUS_CODE_ERROR": "error",
    }
    return mapping.get(code, "unset")


def _resource_spans(payload: Mapping[str, Any]) -> list[Any]:
    nested = payload.get("trace")
    selected = nested if isinstance(nested, Mapping) else payload
    values = selected.get("resourceSpans")
    if not isinstance(values, list):
        values = selected.get("batches")
    return values if isinstance(values, list) else []


def project_trace(
    payload: Mapping[str, Any],
    *,
    trace_id: str,
    max_spans: int,
) -> TraceWaterfall:
    selected_trace_id = validate_trace_id(trace_id)
    parsed: list[dict[str, Any]] = []
    raw_span_count = 0
    for resource_group in _resource_spans(payload):
        if not isinstance(resource_group, Mapping):
            continue
        service = _service(resource_group.get("resource"))
        scope_groups = resource_group.get("scopeSpans")
        if not isinstance(scope_groups, list):
            scope_groups = resource_group.get("instrumentationLibrarySpans")
        if not isinstance(scope_groups, list):
            continue
        for scope_group in scope_groups:
            if not isinstance(scope_group, Mapping):
                continue
            spans = scope_group.get("spans")
            if not isinstance(spans, list):
                continue
            raw_span_count += len(spans)
            for span in spans:
                if not isinstance(span, Mapping):
                    continue
                returned_trace_id = _normalize_binary_id(span.get("traceId"), 16)
                span_id = _normalize_binary_id(span.get("spanId"), 8)
                if returned_trace_id != selected_trace_id or not span_id:
                    continue
                parent_span_id = _normalize_binary_id(span.get("parentSpanId"), 8)
                start = _integer(span.get("startTimeUnixNano"))
                end = _integer(span.get("endTimeUnixNano"))
                if start is None or end is None or end < start:
                    continue
                name = span.get("name")
                safe_name = (
                    name
                    if isinstance(name, str) and name in _SAFE_SPAN_NAMES
                    else "runtime.unknown"
                )
                parsed.append(
                    {
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "name": safe_name,
                        "service": service,
                        "kind": _kind(span.get("kind")),
                        "status": _status(span.get("status")),
                        "start": start,
                        "end": end,
                        "attributes": _attributes(
                            span.get("attributes"), allowlist=SAFE_TRACE_ATTRIBUTES
                        ),
                    }
                )
    parsed.sort(
        key=lambda item: (
            item["start"],
            item["service"],
            item["name"],
            item["span_id"],
        )
    )
    if parsed:
        trace_start = min(item["start"] for item in parsed)
        trace_end = max(item["end"] for item in parsed)
    else:
        trace_start = trace_end = 0
    selected = parsed[:max_spans]
    spans = [
        TraceSpanView(
            span_id=item["span_id"],
            parent_span_id=item["parent_span_id"],
            name=item["name"],
            service=item["service"],
            kind=item["kind"],
            status=item["status"],
            start_offset_ms=round((item["start"] - trace_start) / 1_000_000, 3),
            duration_ms=round((item["end"] - item["start"]) / 1_000_000, 3),
            attributes=item["attributes"],
        )
        for item in selected
    ]
    services = sorted({item["service"] for item in parsed})
    return TraceWaterfall(
        trace_id=selected_trace_id,
        services=services,
        span_count=raw_span_count,
        returned_span_count=len(spans),
        truncated=len(parsed) > len(spans),
        duration_ms=round((trace_end - trace_start) / 1_000_000, 3),
        spans=spans,
    )


class TempoTraceClient:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        max_spans: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_spans = max_spans
        self.transport = transport

    @classmethod
    def from_settings(cls) -> TempoTraceClient:
        return cls(
            enabled=settings.trace_backend_enabled,
            base_url=settings.trace_backend_url,
            timeout_seconds=settings.trace_backend_timeout_seconds,
            max_response_bytes=settings.trace_backend_max_response_bytes,
            max_spans=settings.trace_backend_max_spans,
        )

    async def get(self, trace_id: str) -> TraceWaterfall:
        selected = validate_trace_id(trace_id)
        if not self.enabled:
            raise TraceBackendDisabledError
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "GET",
                    f"{self.base_url}/api/v2/traces/{selected}",
                    headers={"Accept": "application/json"},
                ) as response,
            ):
                if response.status_code == 404:
                    raise TraceNotFoundError
                if response.status_code != 200:
                    raise TraceBackendUnavailableError
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isdecimal()
                    and int(content_length) > self.max_response_bytes
                ):
                    raise TraceBackendResponseTooLargeError
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise TraceBackendResponseTooLargeError
        except (
            TraceNotFoundError,
            TraceBackendUnavailableError,
            TraceBackendResponseTooLargeError,
        ):
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise TraceBackendUnavailableError from exc
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TraceBackendInvalidResponseError from exc
        if not isinstance(payload, Mapping):
            raise TraceBackendInvalidResponseError
        try:
            return project_trace(payload, trace_id=selected, max_spans=self.max_spans)
        except (TypeError, ValueError) as exc:
            raise TraceBackendInvalidResponseError from exc


tempo_trace_client = TempoTraceClient.from_settings()
