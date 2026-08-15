"""Persistent models for durable Agent execution."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse

AgentRunStatus = Literal[
    "queued",
    "running",
    "retry",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_AGENT_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {"completed", "failed", "cancelled"}
)


class AgentRunRecord(BaseModel):
    run_id: str
    idempotency_key: str
    request_fingerprint: str
    request_json: str
    traceparent: str = ""
    result_json: str | None = None
    status: AgentRunStatus = "queued"
    attempts: int = 0
    max_attempts: int = 2
    available_at: str
    lease_until: str | None = None
    worker_id: str | None = None
    cancel_requested_at: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None

    @property
    def request(self) -> AgentQueryRequest:
        return AgentQueryRequest.model_validate_json(self.request_json)

    @property
    def result(self) -> AgentQueryResponse | None:
        if not self.result_json:
            return None
        return AgentQueryResponse.model_validate_json(self.result_json)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_AGENT_RUN_STATUSES

    @property
    def trace_id(self) -> str | None:
        from agent_rag.tracing.runtime import trace_id_from_traceparent

        return trace_id_from_traceparent(self.traceparent)


class AgentRunEvent(BaseModel):
    event_id: int
    run_id: str
    attempt: int
    event_type: str
    payload_json: str
    created_at: str

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        return value if isinstance(value, dict) else {"value": value}
