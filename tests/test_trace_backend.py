from __future__ import annotations

import base64
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_rag.api.routes import telemetry_router
from agent_rag.config import Settings
from agent_rag.security.auth import ApiKeyAuthenticator
from agent_rag.security.credentials import hash_api_key
from agent_rag.tracing.backend import (
    TempoTraceClient,
    TraceBackendDisabledError,
    TraceBackendInvalidResponseError,
    TraceBackendResponseTooLargeError,
    TraceBackendUnavailableError,
    TraceNotFoundError,
    TraceSpanView,
    TraceWaterfall,
    project_trace,
    validate_trace_id,
)

TRACE_ID = "11111111111111111111111111111111"
ROOT_SPAN = "2222222222222222"
CHILD_SPAN = "3333333333333333"


def _any_string(value: str) -> dict:
    return {"stringValue": value}


def _payload(*, base64_ids: bool = False) -> dict:
    def selected(value: str) -> str:
        if not base64_ids:
            return value
        return base64.b64encode(bytes.fromhex(value)).decode()

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": _any_string("polyuquest-api")},
                        {"key": "secret.resource", "value": _any_string("resource-canary")},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "private-scope"},
                        "spans": [
                            {
                                "traceId": selected(TRACE_ID),
                                "spanId": selected(ROOT_SPAN),
                                "name": "agent.run.submit",
                                "kind": "SPAN_KIND_PRODUCER",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1100000000",
                                "status": {"code": "STATUS_CODE_OK"},
                                "attributes": [
                                    {"key": "attempt", "value": {"intValue": "1"}},
                                    {"key": "query", "value": _any_string("query-canary")},
                                ],
                            },
                            {
                                "traceId": selected(TRACE_ID),
                                "spanId": selected(CHILD_SPAN),
                                "parentSpanId": selected(ROOT_SPAN),
                                "name": "dynamic/private/path",
                                "kind": 5,
                                "startTimeUnixNano": "1120000000",
                                "endTimeUnixNano": "1320000000",
                                "status": {
                                    "code": 2,
                                    "message": "status-message-canary",
                                },
                                "attributes": [
                                    {"key": "evidence.count", "value": {"intValue": 3}},
                                    {"key": "url.full", "value": _any_string("url-canary")},
                                ],
                                "events": [
                                    {
                                        "name": "exception",
                                        "attributes": [
                                            {
                                                "key": "exception.message",
                                                "value": _any_string("event-canary"),
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize("base64_ids", [False, True])
def test_projection_builds_bounded_privacy_safe_waterfall(base64_ids: bool) -> None:
    result = project_trace(_payload(base64_ids=base64_ids), trace_id=TRACE_ID, max_spans=10)

    assert result.trace_id == TRACE_ID
    assert result.services == ["polyuquest-api"]
    assert result.span_count == 2
    assert result.returned_span_count == 2
    assert result.duration_ms == 320
    assert result.spans[0].name == "agent.run.submit"
    assert result.spans[1].name == "runtime.unknown"
    assert result.spans[1].parent_span_id == ROOT_SPAN
    assert result.spans[1].start_offset_ms == 120
    assert result.spans[1].duration_ms == 200
    assert result.spans[1].attributes == {"evidence.count": 3}
    serialized = result.model_dump_json()
    for canary in (
        "resource-canary",
        "query-canary",
        "status-message-canary",
        "url-canary",
        "event-canary",
        "private-scope",
        "dynamic/private/path",
    ):
        assert canary not in serialized


def test_projection_is_deterministic_and_truncates() -> None:
    result = project_trace(_payload(), trace_id=TRACE_ID.upper(), max_spans=1)
    assert result.returned_span_count == 1
    assert result.span_count == 2
    assert result.truncated is True
    assert result.spans[0].span_id == ROOT_SPAN


@pytest.mark.parametrize(
    "value",
    ["", "abc", "0" * 32, "g" * 32, "1" * 31, "1" * 33],
)
def test_trace_id_validation_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_trace_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "file:///private/tempo",
        "http://user:secret@tempo:3200",
        "http://tempo:3200/private",
        "http://tempo:3200?trace=private",
        "//tempo:3200",
    ],
)
def test_settings_reject_trace_backend_ssrf_shapes(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trace_backend_url=value)


def _client(handler, *, enabled: bool = True, max_bytes: int = 100_000) -> TempoTraceClient:
    return TempoTraceClient(
        enabled=enabled,
        base_url="http://tempo:3200",
        timeout_seconds=1,
        max_response_bytes=max_bytes,
        max_spans=10,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_client_uses_only_fixed_tempo_path_and_returns_projection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload())

    result = await _client(handler).get(TRACE_ID.upper())
    assert result.returned_span_count == 2
    assert seen[0].url == f"http://tempo:3200/api/v2/traces/{TRACE_ID}"
    assert seen[0].headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_client_maps_backend_failure_contracts() -> None:
    with pytest.raises(TraceBackendDisabledError):
        await _client(lambda _request: httpx.Response(200), enabled=False).get(TRACE_ID)
    with pytest.raises(TraceNotFoundError):
        await _client(lambda _request: httpx.Response(404)).get(TRACE_ID)
    with pytest.raises(TraceBackendUnavailableError):
        await _client(lambda _request: httpx.Response(500, text="private-error")).get(
            TRACE_ID
        )
    with pytest.raises(TraceBackendInvalidResponseError):
        await _client(lambda _request: httpx.Response(200, content=b"not-json")).get(
            TRACE_ID
        )
    oversized = json.dumps(_payload()).encode()
    with pytest.raises(TraceBackendResponseTooLargeError):
        await _client(
            lambda _request: httpx.Response(200, content=oversized),
            max_bytes=32,
        ).get(TRACE_ID)


@pytest.mark.asyncio
async def test_client_maps_transport_errors_without_leaking_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-backend-host", request=request)

    with pytest.raises(TraceBackendUnavailableError) as raised:
        await _client(handler).get(TRACE_ID)
    assert "private-backend-host" not in str(raised.value)


def _auth_records() -> str:
    return ",".join(
        (
            f"reader:reader:{hash_api_key('reader-secret')}",
            f"operator:operator:{hash_api_key('operator-secret')}",
            f"admin:admin:{hash_api_key('admin-secret')}",
        )
    )


def test_trace_api_is_operator_only_and_never_exposes_backend_payload() -> None:
    class FakeClient:
        async def get(self, trace_id: str) -> TraceWaterfall:
            return TraceWaterfall(
                trace_id=trace_id,
                services=["polyuquest-api"],
                span_count=1,
                returned_span_count=1,
                truncated=False,
                duration_ms=1,
                spans=[
                    TraceSpanView(
                        span_id=ROOT_SPAN,
                        name="agent.run.submit",
                        service="polyuquest-api",
                        kind="producer",
                        status="ok",
                        start_offset_ms=0,
                        duration_ms=1,
                    )
                ],
            )

    app = FastAPI()
    app.include_router(telemetry_router.router, prefix="/api")
    authenticator = ApiKeyAuthenticator("api_key", _auth_records())
    with (
        patch("agent_rag.security.auth.security_authenticator", authenticator),
        patch.object(telemetry_router, "tempo_trace_client", FakeClient()),
    ):
        client = TestClient(app)
        reader = client.get(
            f"/api/telemetry/traces/{TRACE_ID}",
            headers={"X-API-Key": "reader-secret"},
        )
        operator = client.get(
            f"/api/telemetry/traces/{TRACE_ID}",
            headers={"X-API-Key": "operator-secret"},
        )

    assert reader.status_code == 403
    assert operator.status_code == 200
    assert operator.headers["cache-control"] == "no-store"
    assert operator.json()["spans"][0]["name"] == "agent.run.submit"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (TraceBackendDisabledError(), 503, "trace_backend_disabled"),
        (TraceNotFoundError(), 404, "Trace not found"),
        (TraceBackendUnavailableError("private-host"), 503, "trace_backend_unavailable"),
        (
            TraceBackendResponseTooLargeError(),
            502,
            "trace_backend_response_too_large",
        ),
        (
            TraceBackendInvalidResponseError("private-payload"),
            502,
            "trace_backend_invalid_response",
        ),
    ],
)
def test_trace_api_maps_backend_failures_to_stable_safe_errors(
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    class FailingClient:
        async def get(self, _trace_id: str) -> TraceWaterfall:
            raise error

    app = FastAPI()
    app.include_router(telemetry_router.router, prefix="/api")
    authenticator = ApiKeyAuthenticator("api_key", _auth_records())
    with (
        patch("agent_rag.security.auth.security_authenticator", authenticator),
        patch.object(telemetry_router, "tempo_trace_client", FailingClient()),
    ):
        response = TestClient(app).get(
            f"/api/telemetry/traces/{TRACE_ID}",
            headers={"X-API-Key": "operator-secret"},
        )

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": expected_detail}
    assert "private" not in response.text
