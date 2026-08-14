"""Contracts for end-to-end run and span telemetry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RunType = Literal["agent_query", "indexing", "reconciliation", "evaluation"]
RunStatus = Literal["running", "completed", "error", "cancelled"]
SpanStatus = Literal["succeeded", "failed", "skipped"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunTelemetry(BaseModel):
    run_id: str
    run_type: RunType
    root_run_id: str
    parent_run_id: str | None = None
    status: RunStatus = "running"
    response_status: str = ""
    route_mode: str = ""
    stop_reason: str = ""
    query_hash: str = ""
    query_length: int = 0
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    duration_ms: int = 0
    logical_input_tokens: int = 0
    logical_output_tokens: int = 0
    billable_input_tokens: int = 0
    billable_output_tokens: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    evidence_count: int = 0
    pages_fetched: int = 0
    fetch_failures: int = 0
    indexing_jobs_queued: int = 0
    error_category: str = ""
    error: str = ""
    config_fingerprint: str = ""
    code_version: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class TelemetrySpan(BaseModel):
    span_id: str
    run_id: str
    stage: str
    operation: str
    status: SpanStatus
    started_at: str = Field(default_factory=utc_now)
    duration_ms: int = 0
    logical_input_tokens: int = 0
    logical_output_tokens: int = 0
    billable_input_tokens: int = 0
    billable_output_tokens: int = 0
    llm_calls: int = 0
    cache_hit: bool = False
    provider: str = ""
    model: str = ""
    error_category: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class RunTelemetryDetail(BaseModel):
    run: RunTelemetry
    spans: list[TelemetrySpan] = Field(default_factory=list)
