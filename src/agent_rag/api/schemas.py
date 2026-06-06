"""Pydantic models for API request/response."""

from __future__ import annotations

from pydantic import BaseModel


class Turn(BaseModel):
    user: str
    assistant: str


class QueryRequest(BaseModel):
    query: str
    mode: str | None = None
    history: list[Turn] | None = None


class BlockRef(BaseModel):
    block_id: str
    content: str
    heading_context: str
    source_url: str
    source_title: str
    score: float


class PipelineStep(BaseModel):
    step: str
    label: str
    duration_ms: int
    data: dict = {}


class QueryResponse(BaseModel):
    answer: str
    mode: str
    routing_reasoning: str = ""
    blocks: list[BlockRef]
    elapsed_seconds: float
    sub_queries: list[dict] | None = None
    keywords_extracted: list[str] | None = None
    entities_expanded: int | None = None
    pipeline_trace: list[PipelineStep] = []


class GraphStatsResponse(BaseModel):
    webpages: int
    blocks: int
    entities: int
    topic_keywords: int
    links_to: int
    contains: int
    relates_to: int
    extracted_from: int


class HealthResponse(BaseModel):
    status: str
    neo4j: bool
    qdrant: bool


class GraphDataRequest(BaseModel):
    center_entity: str | None = None
    max_nodes: int = 100


class GraphNode(BaseModel):
    id: str
    label: str
    type: str  # WebPage, Block, Entity, TopicKeyword
    properties: dict = {}


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str
    properties: dict = {}


class GraphDataResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
