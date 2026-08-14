"""Versioned dataset, observation, and report schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    oracle_type: Literal["semantic_gold", "behavioral_contract", "smoke"] | None = None
    criticality: Literal["blocker", "critical", "standard"] = "standard"
    annotation_owner: str = ""
    reviewed_at: str | None = None

    @model_validator(mode="after")
    def _infer_oracle_type(self) -> EvaluationCase:
        if self.oracle_type is not None:
            return self
        if self.required_facts or self.required_source_patterns or self.forbidden_claims:
            self.oracle_type = "semantic_gold"
        elif (
            self.expected_status is not None
            or self.expected_exploration != "either"
            or self.expected_persistence != "either"
            or self.max_elapsed_seconds is not None
            or self.max_pages_fetched is not None
        ):
            self.oracle_type = "behavioral_contract"
        else:
            self.oracle_type = "smoke"
        return self


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
    tags: list[str] = Field(default_factory=list)
    task_type: str = ""
    oracle_type: str = "smoke"
    criticality: str = "standard"
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
    elapsed_seconds: float = 0.0
    billable_tokens: int | None = None
    pages_fetched: int = 0


class SliceSummary(BaseModel):
    case_count: int
    missing_responses: int
    semantic_gold_cases: int
    quality_score: float | None = None
    operational_score: float | None = None
    overall: float | None = None
    avg_elapsed_seconds: float | None = None
    avg_billable_tokens: float | None = None


class EvaluationDatasetManifest(BaseModel):
    schema_version: str = "1.0"
    dataset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1)
    description: str = ""
    owner: str = Field(min_length=1)
    domain_scope: list[str] = Field(default_factory=list)
    status: Literal["draft", "reviewed", "approved", "retired"] = "draft"
    case_file: str = Field(min_length=1)
    case_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str
    source_snapshot_at: str | None = None
    reviewer: str = ""
    reviewed_at: str | None = None
    minimum_cases: int = Field(default=1, ge=1)
    minimum_semantic_gold_ratio: float = Field(default=0.0, ge=0, le=1)
    required_tags: list[str] = Field(default_factory=list)


class DatasetValidationResult(BaseModel):
    valid: bool
    manifest_path: str
    dataset_id: str = ""
    version: str = ""
    status: str = ""
    dataset_fingerprint: str = ""
    case_file_sha256: str = ""
    case_count: int = 0
    semantic_gold_cases: int = 0
    behavioral_contract_cases: int = 0
    smoke_cases: int = 0
    semantic_gold_ratio: float = 0.0
    blocker_cases: int = 0
    critical_cases: int = 0
    tag_counts: dict[str, int] = Field(default_factory=dict)
    task_type_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GateEvidencePolicy(BaseModel):
    allowed_dataset_statuses: list[str] = Field(default_factory=lambda: ["approved"])
    minimum_cases: int = Field(default=20, ge=1)
    minimum_semantic_gold_cases: int = Field(default=10, ge=0)
    minimum_semantic_gold_ratio: float = Field(default=0.5, ge=0, le=1)


class GateMetricFloor(BaseModel):
    metric: str
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _one_boundary(self) -> GateMetricFloor:
        if (self.minimum is None) == (self.maximum is None):
            raise ValueError("metric floor requires exactly one of minimum or maximum")
        return self


class GateRegressionLimit(BaseModel):
    metric: str
    direction: Literal["higher_better", "lower_better"]
    max_degradation: float = Field(ge=0)


class GateCostRatioLimit(BaseModel):
    metric: str
    maximum_ratio: float = Field(ge=1)


class GateCriticalPolicy(BaseModel):
    case_metric: str = "overall"
    max_case_degradation: float = Field(default=0.0, ge=0)
    max_blocker_regressions: int = Field(default=0, ge=0)
    max_critical_regressions: int = Field(default=0, ge=0)


class GateSlicePolicy(BaseModel):
    minimum_cases: int = Field(default=3, ge=1)
    max_quality_degradation: float = Field(default=0.05, ge=0)
    max_operational_degradation: float = Field(default=0.05, ge=0)
    required_slices: list[str] = Field(default_factory=list)


class ReleaseGatePolicy(BaseModel):
    schema_version: str = "1.0"
    policy_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1)
    missing_metric_policy: Literal["fail", "insufficient_evidence", "ignore"] = (
        "insufficient_evidence"
    )
    evidence: GateEvidencePolicy = Field(default_factory=GateEvidencePolicy)
    candidate_floors: list[GateMetricFloor] = Field(default_factory=list)
    regression_limits: list[GateRegressionLimit] = Field(default_factory=list)
    cost_ratio_limits: list[GateCostRatioLimit] = Field(default_factory=list)
    critical: GateCriticalPolicy = Field(default_factory=GateCriticalPolicy)
    slices: GateSlicePolicy = Field(default_factory=GateSlicePolicy)


class GateCheck(BaseModel):
    category: str
    metric: str
    scope: str = "global"
    outcome: Literal["pass", "fail", "insufficient", "warning"]
    actual: float | int | str | None = None
    expected: float | int | str | None = None
    message: str


class GateDecision(BaseModel):
    schema_version: str = "1.0"
    gate_id: str
    status: Literal["pass", "fail", "insufficient_evidence"]
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    decision_fingerprint: str
    dataset_id: str = ""
    dataset_version: str = ""
    dataset_status: str = ""
    dataset_fingerprint: str
    baseline_variant: str
    candidate_variant: str
    baseline_report_fingerprint: str
    candidate_report_fingerprint: str
    baseline_summary: dict[str, float | int | None] = Field(default_factory=dict)
    candidate_summary: dict[str, float | int | None] = Field(default_factory=dict)
    summary_deltas: dict[str, float | None] = Field(default_factory=dict)
    failed_checks: int
    insufficient_checks: int
    warning_checks: int
    checks: list[GateCheck]


class EvaluationReport(BaseModel):
    schema_version: str = "1.1"
    evaluator_version: str = "deterministic-v2"
    variant: str
    dataset_fingerprint: str
    evaluation_config_fingerprint: str
    system_config_fingerprints: list[str] = Field(default_factory=list)
    code_versions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    cases: int
    dataset_id: str = ""
    dataset_version: str = ""
    dataset_status: str = ""
    summary: dict[str, float | int | None]
    slices: dict[str, SliceSummary] = Field(default_factory=dict)
    results: list[CaseScore]
