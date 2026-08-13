"""Public request/response models for the autonomous retrieval endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agent_rag.config import agent_config
from agent_rag.tools.schemas import EvidenceBlock, SearchMode, ToolTraceStep

_CONTROLLER = agent_config.get("controller", {})


class AgentTurn(BaseModel):
    user: str
    assistant: str


class AgentBudget(BaseModel):
    max_iterations: int = Field(
        default=int(_CONTROLLER.get("max_iterations", 3)), ge=1, le=8
    )
    max_pages: int = Field(default=int(_CONTROLLER.get("max_pages", 5)), ge=0, le=20)
    max_depth: int = Field(default=int(_CONTROLLER.get("max_depth", 2)), ge=1, le=4)
    max_seconds: int = Field(
        default=int(_CONTROLLER.get("max_seconds", 90)), ge=5, le=300
    )


class AgentQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    mode: SearchMode = "auto"
    history: list[AgentTurn] = Field(default_factory=list)
    explore_web: bool = True
    persist_discoveries: bool = False
    freshness: Literal["auto", "prefer_fresh", "require_fresh"] = "auto"
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @model_validator(mode="after")
    def _persistence_requires_exploration(self) -> AgentQueryRequest:
        if self.persist_discoveries and not self.explore_web:
            raise ValueError("persist_discoveries requires explore_web=true")
        return self


class AgentAction(BaseModel):
    sequence: int
    action: str
    status: Literal["started", "succeeded", "failed", "skipped"]
    duration_ms: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class ExplorationSummary(BaseModel):
    iterations: int = 0
    pages_fetched: int = 0
    fetch_failures: int = 0
    frontier_candidates_seen: int = 0
    temporary_evidence_blocks: int = 0
    patches_published: int = 0
    stop_reason: str = ""


class AgentQueryResponse(BaseModel):
    run_id: str
    answer: str
    response_status: Literal["answered", "partial", "abstained", "error"]
    mode: str
    evidence: list[EvidenceBlock] = Field(default_factory=list)
    actions: list[AgentAction] = Field(default_factory=list)
    exploration: ExplorationSummary = Field(default_factory=ExplorationSummary)
    pipeline_trace: list[ToolTraceStep] = Field(default_factory=list)
    elapsed_seconds: float = 0.0
