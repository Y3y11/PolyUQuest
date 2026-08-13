"""Pydantic contracts shared by agent tools and API responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ToolTraceStep(BaseModel):
    step: str
    label: str
    duration_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class RouteDecision(BaseModel):
    mode: str
    confidence: float = 1.0
    reasoning: str = ""
    source: str = "unknown"
    alt_mode: str | None = None


class EvidenceScores(BaseModel):
    retrieval: float = 0.0
    reranker: float | None = None
    bm25: float | None = None


class QueryConstraint(BaseModel):
    """An open-domain constraint extracted from the user's query."""

    kind: Literal["entity", "qualifier"]
    label: str
    field: str = ""
    value: str = ""
    aliases: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    required: bool = True


class EvidenceRequirement(BaseModel):
    """A claim the answer must support plus portable surface-form cues."""

    claim: str
    target_cues: list[str] = Field(default_factory=list)
    evidence_cues: list[str] = Field(default_factory=list)
    # Backward-compatible field for profiles cached before target/evidence
    # signals were separated.
    cues: list[str] = Field(default_factory=list)
    required: bool = True


class QueryProfile(BaseModel):
    """Structured query requirements shared by ranking and evidence checks."""

    query: str
    constraints: list[QueryConstraint] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    required_claims: list[EvidenceRequirement] = Field(default_factory=list)
    source: Literal["deterministic", "llm_enriched", "fallback"] = "deterministic"


class EvidenceBlock(BaseModel):
    block_id: str
    content: str
    heading_context: str = ""
    source_url: str
    source_title: str = ""
    page_type: str = "other"
    fetched_at: str | None = None
    content_hash: str | None = None
    scores: EvidenceScores = Field(default_factory=EvidenceScores)
    matched_entities: list[str] = Field(default_factory=list)
    supports_sub_goals: list[str] = Field(default_factory=list)
    observation_id: str | None = None
    temporary: bool = False


class FrontierSeed(BaseModel):
    url: str
    parent_url: str | None = None
    anchor_text: str = ""
    title: str = ""
    edge_type: str = "LINKS_TO"
    graph_distance: int = 1
    supports_sub_goals: list[str] = Field(default_factory=list)
    already_indexed: bool = False
    last_fetched_at: str | None = None
    score: float = 0.0


SearchMode = Literal["auto", "block", "navigation", "entity", "hybrid"]


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    query_profile: QueryProfile | None = None
    sub_goal_id: str = "goal-0"
    mode: SearchMode = "auto"
    top_k: int = Field(default=8, ge=1, le=50)
    page_types: list[str] = Field(default_factory=list)
    freshness_after: datetime | None = None
    include_frontier_seeds: bool = True
    history: list[dict[str, str]] = Field(default_factory=list)


class SearchOutput(BaseModel):
    observation_id: str
    route: RouteDecision
    evidence: list[EvidenceBlock] = Field(default_factory=list)
    frontier_seeds: list[FrontierSeed] = Field(default_factory=list)
    trace: list[ToolTraceStep] = Field(default_factory=list)
    elapsed_ms: int = 0
    answer_prompt: str = Field(default="", exclude=True)


class ExpandInput(BaseModel):
    sub_goal_id: str = "goal-0"
    query: str = Field(min_length=1, max_length=4000)
    query_profile: QueryProfile | None = None
    source_block_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    max_candidates: int = Field(default=20, ge=1, le=100)


class ExpandOutput(BaseModel):
    candidates: list[FrontierSeed] = Field(default_factory=list)
    trace: list[ToolTraceStep] = Field(default_factory=list)


class FetchInput(BaseModel):
    url: HttpUrl
    sub_goal_id: str = "goal-0"
    query: str = Field(min_length=1, max_length=4000)
    query_profile: QueryProfile | None = None
    run_id: str
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    max_bytes: int = Field(default=5_000_000, ge=10_000, le=20_000_000)
    if_none_match: str | None = None
    if_modified_since: str | None = None


class FetchMetadata(BaseModel):
    requested_url: str
    final_url: str
    title: str = ""
    meta_description: str = ""
    department: str = ""
    page_type: str = "other"
    fetched_at: str
    content_hash: str
    etag: str | None = None
    last_modified: str | None = None
    status_code: int


class EvidenceGain(BaseModel):
    relevant_blocks: int = 0
    total_blocks: int = 0
    lexical_coverage: float = 0.0


class FetchOutput(BaseModel):
    observation_id: str
    metadata: FetchMetadata
    block_refs: list[str] = Field(default_factory=list)
    discovered_links: list[FrontierSeed] = Field(default_factory=list)
    evidence_gain: EvidenceGain = Field(default_factory=EvidenceGain)
    not_modified: bool = False
    trace: list[ToolTraceStep] = Field(default_factory=list)


PatchStatus = Literal["staged", "publishing", "published", "repair_required", "failed"]


class StagePatchInput(BaseModel):
    observation_id: str
    run_id: str
    persist_level: Literal["blocks_only"] = "blocks_only"


class GraphPatch(BaseModel):
    patch_id: str
    observation_id: str
    run_id: str
    source_url: str
    content_hash: str
    persist_level: Literal["blocks_only"] = "blocks_only"
    status: PatchStatus = "staged"
    created_at: str
    updated_at: str
    error: str | None = None


class PublishPatchInput(BaseModel):
    patch_id: str


class PublishPatchOutput(BaseModel):
    patch: GraphPatch
    webpages_written: int = 0
    blocks_written: int = 0
    links_written: int = 0
    read_after_write_ok: bool = False
