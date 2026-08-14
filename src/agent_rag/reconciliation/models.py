"""Persistent models for cross-store consistency governance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


RunStatus = Literal["scanning", "planned", "executing", "completed", "failed"]
ActionStatus = Literal["planned", "running", "succeeded", "failed", "skipped"]
Severity = Literal["info", "warning", "critical"]
Repairability = Literal["automatic", "manual_review", "informational"]
ActionType = Literal["replay_patch", "retry_index_job"]


class CurrentFactState(BaseModel):
    fact_key: str
    source_block_ids: list[str] = Field(default_factory=list)
    page_version_id: str = ""


class ConsistencyInventory(BaseModel):
    neo4j_ids: dict[str, set[str]] = Field(default_factory=dict)
    qdrant_ids: dict[str, set[str]] = Field(default_factory=dict)
    neo4j_patch_ids: dict[str, dict[str, str]] = Field(default_factory=dict)
    qdrant_patch_ids: dict[str, dict[str, str]] = Field(default_factory=dict)
    non_vector_webpage_ids: set[str] = Field(default_factory=set)
    neo4j_facts: dict[str, CurrentFactState] = Field(default_factory=dict)
    qdrant_fact_sources: dict[str, list[str]] = Field(default_factory=dict)


class ConsistencyFinding(BaseModel):
    finding_id: str = Field(default_factory=lambda: f"finding-{uuid.uuid4().hex}")
    run_id: str
    category: str
    severity: Severity
    object_type: str
    object_id: str
    source_url: str = ""
    patch_id: str = ""
    version_id: str = ""
    job_id: str = ""
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    reason: str
    repairability: Repairability
    recommended_action: str = ""
    created_at: str = Field(default_factory=utc_now)


class RepairAction(BaseModel):
    action_id: str = Field(default_factory=lambda: f"repair-{uuid.uuid4().hex}")
    run_id: str
    finding_id: str
    action_type: ActionType
    target_id: str
    status: ActionStatus = "planned"
    requires_confirmation: bool = True
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    execution_owner_id: str | None = None


class ReconciliationRun(BaseModel):
    run_id: str = Field(default_factory=lambda: f"recon-{uuid.uuid4().hex}")
    status: RunStatus = "scanning"
    scope: str = "all"
    verification_of_run_id: str | None = None
    findings_count: int = 0
    actions_count: int = 0
    automatic_count: int = 0
    manual_review_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    scan_completed_at: str | None = None
    execution_started_at: str | None = None
    execution_lease_until: str | None = None
    completed_at: str | None = None


class ReconciliationRunDetail(BaseModel):
    run: ReconciliationRun
    findings: list[ConsistencyFinding] = Field(default_factory=list)
    actions: list[RepairAction] = Field(default_factory=list)
