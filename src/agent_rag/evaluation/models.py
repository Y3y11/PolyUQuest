"""Versioned dataset, observation, and report schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class EvaluationCase(BaseModel):
    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    task_type: str = ""
    expected_status: Literal["answered", "partial", "abstained", "error"] | None = None
    required_facts: list[list[str]] = Field(default_factory=list)
    required_source_patterns: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    expected_exploration: Literal["required", "forbidden", "either"] = "either"
    expected_persistence: Literal["required", "forbidden", "either"] = "either"
    max_elapsed_seconds: float | None = None
    max_pages_fetched: int | None = None


class ObservedResponse(BaseModel):
    case_id: str
    response_status: str
    answer: str = ""
    evidence_urls: list[str] = Field(default_factory=list)
    pages_fetched: int = 0
    fetch_failures: int = 0
    indexing_jobs_queued: int = 0
    elapsed_seconds: float = 0.0
    billable_tokens: int | None = None
    system_config_fingerprint: str = ""
    code_version: str = ""
    run_id: str = ""


class CaseScore(BaseModel):
    case_id: str
    status_match: float | None = None
    fact_coverage: float | None = None
    source_recall: float | None = None
    forbidden_claim_rate: float | None = None
    exploration_match: float | None = None
    persistence_match: float | None = None
    latency_budget_match: float | None = None
    page_budget_match: float | None = None
    quality_score: float | None = None
    operational_score: float | None = None
    overall: float | None = None
    failures: list[str] = Field(default_factory=list)
    run_id: str = ""


class EvaluationReport(BaseModel):
    schema_version: str = "1.0"
    evaluator_version: str = "deterministic-v1"
    variant: str
    dataset_fingerprint: str
    evaluation_config_fingerprint: str
    system_config_fingerprints: list[str] = Field(default_factory=list)
    code_versions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    cases: int
    summary: dict[str, float | int | None]
    results: list[CaseScore]
