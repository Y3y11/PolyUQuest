"""Typed adapter around the existing PolyUQuest retrieval pipelines."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from agent_rag.config import thresholds_config
from agent_rag.llm.client import LLMClient
from agent_rag.retrieval._embedding import embed_query
from agent_rag.retrieval._rewriter import reset_router_confidence, set_router_confidence
from agent_rag.retrieval.direct import retrieve_direct
from agent_rag.retrieval.hybrid import retrieve_hybrid
from agent_rag.retrieval.navigation import retrieve_navigation
from agent_rag.retrieval.reasoning import retrieve_reasoning
from agent_rag.retrieval.router import route_query
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore
from agent_rag.tools.expand import ExpandTool
from agent_rag.tools.schemas import (
    EvidenceBlock,
    EvidenceScores,
    ExpandInput,
    RouteDecision,
    SearchInput,
    SearchOutput,
    ToolTraceStep,
)

_MODE_MAP = {"block": "mode_a", "navigation": "mode_b", "entity": "mode_c"}
_RETRIEVERS = {
    "mode_a": retrieve_direct,
    "mode_b": retrieve_navigation,
    "mode_c": retrieve_reasoning,
}
_HYBRID_THRESHOLD = float(
    thresholds_config.get("retrieval", {}).get("router", {}).get("hybrid_threshold", 0.6)
)


class SearchTool:
    name = "polyuquest.search"

    def __init__(
        self,
        neo4j_factory: Callable[[], Neo4jStore] = Neo4jStore,
        qdrant_factory: Callable[[], QdrantStore] = QdrantStore,
        llm_factory: Callable[[], LLMClient] = LLMClient,
        embedder: Callable[[str], list[float]] = embed_query,
        expand_tool: ExpandTool | None = None,
    ):
        self._neo4j_factory = neo4j_factory
        self._qdrant_factory = qdrant_factory
        self._llm_factory = llm_factory
        self._embedder = embedder
        self._expand_tool = expand_tool or ExpandTool(neo4j_factory)

    def run(self, tool_input: SearchInput) -> SearchOutput:
        started = time.perf_counter()
        neo4j = None
        qdrant = None
        llm = None
        try:
            neo4j = self._neo4j_factory()
            qdrant = self._qdrant_factory()
            llm = self._llm_factory()
            routing_started = time.perf_counter()
            if tool_input.mode == "auto":
                routing = route_query(tool_input.query, llm=llm)
                mode = routing.get("mode", "mode_a")
                confidence = float(routing.get("confidence", 1.0))
                alt_mode = routing.get("alt_mode")
                hybrid = bool(
                    alt_mode and confidence < _HYBRID_THRESHOLD and mode != alt_mode
                )
                hybrid_modes = [mode, alt_mode] if hybrid else []
            elif tool_input.mode == "hybrid":
                routing = {
                    "mode": "hybrid",
                    "confidence": 1.0,
                    "source": "forced",
                    "reasoning": "Hybrid mode was requested by the caller.",
                }
                mode, confidence, alt_mode = "hybrid", 1.0, None
                hybrid, hybrid_modes = True, ["mode_a", "mode_b", "mode_c"]
            else:
                mode = _MODE_MAP[tool_input.mode]
                confidence, alt_mode, hybrid = 1.0, None, False
                hybrid_modes = []
                routing = {
                    "mode": mode,
                    "confidence": confidence,
                    "source": "forced",
                    "reasoning": f"{tool_input.mode} mode was requested by the caller.",
                }
            routing_ms = int((time.perf_counter() - routing_started) * 1000)

            embedding_started = time.perf_counter()
            query_embedding = self._embedder(tool_input.query)
            embedding_ms = int((time.perf_counter() - embedding_started) * 1000)
            confidence_token = set_router_confidence(confidence)
            try:
                if hybrid:
                    result = retrieve_hybrid(
                        tool_input.query,
                        query_embedding,
                        neo4j,
                        qdrant,
                        llm,
                        modes=hybrid_modes,
                        skip_answer=True,
                        history=tool_input.history,
                    )
                else:
                    fn = _RETRIEVERS.get(mode, retrieve_direct)
                    kwargs: dict[str, Any] = {
                        "skip_answer": True,
                        "history": tool_input.history,
                    }
                    if mode == "mode_a":
                        kwargs["top_k"] = tool_input.top_k
                    elif mode == "mode_b":
                        kwargs["top_k_blocks"] = tool_input.top_k
                    result = fn(
                        tool_input.query, query_embedding, neo4j, qdrant, llm, **kwargs
                    )
            finally:
                reset_router_confidence(confidence_token)

            raw_blocks = result.get("blocks", [])
            block_ids = [b.get("block_id", "") for b in raw_blocks if b.get("block_id")]
            pages = neo4j.get_webpages_for_blocks_batch(block_ids)
            evidence: list[EvidenceBlock] = []
            for block in raw_blocks:
                block_id = block.get("block_id", "")
                page = pages.get(block_id, {})
                page_type = page.get("page_type", "other")
                if tool_input.page_types and page_type not in tool_input.page_types:
                    continue
                fetched_at = page.get("fetched_at") or page.get("last_crawled")
                if tool_input.freshness_after and fetched_at:
                    try:
                        if str(fetched_at) < tool_input.freshness_after.isoformat():
                            continue
                    except (TypeError, ValueError):
                        pass
                reranker_score = block.get("rerank_score")
                retrieval_score = float(block.get("score", reranker_score or 0.0) or 0.0)
                evidence.append(
                    EvidenceBlock(
                        block_id=block_id,
                        content=block.get("content", ""),
                        heading_context=block.get("heading_context", ""),
                        source_url=block.get("source_url") or page.get("url", ""),
                        source_title=block.get("source_title") or page.get("title", ""),
                        page_type=page_type,
                        fetched_at=fetched_at,
                        content_hash=page.get("content_hash"),
                        scores=EvidenceScores(
                            retrieval=retrieval_score,
                            reranker=(
                                float(reranker_score) if reranker_score is not None else None
                            ),
                            bm25=block.get("bm25_score"),
                        ),
                        supports_sub_goals=[tool_input.sub_goal_id],
                    )
                )
                if len(evidence) >= tool_input.top_k:
                    break

            frontier = []
            expand_trace: list[ToolTraceStep] = []
            if tool_input.include_frontier_seeds and evidence:
                expanded = self._expand_tool.run(
                    ExpandInput(
                        query=tool_input.query,
                        query_profile=tool_input.query_profile,
                        sub_goal_id=tool_input.sub_goal_id,
                        source_block_ids=[item.block_id for item in evidence],
                        source_urls=[item.source_url for item in evidence],
                        max_candidates=min(20, tool_input.top_k * 3),
                    )
                )
                frontier = expanded.candidates
                expand_trace = expanded.trace

            trace = [
                ToolTraceStep(
                    step="routing",
                    label="Query routing",
                    duration_ms=routing_ms,
                    data={
                        "mode": mode,
                        "alt_mode": alt_mode,
                        "confidence": confidence,
                        "hybrid_triggered": hybrid,
                        "source": routing.get("source", "unknown"),
                    },
                ),
                ToolTraceStep(
                    step="query_embedding",
                    label="Query embedding",
                    duration_ms=embedding_ms,
                    data={"dimensions": len(query_embedding)},
                ),
                *[ToolTraceStep(**step) for step in result.get("trace", [])],
                *expand_trace,
            ]
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return SearchOutput(
                observation_id=f"search-{uuid.uuid4().hex}",
                route=RouteDecision(
                    mode=result.get("mode", mode),
                    confidence=confidence,
                    reasoning=routing.get("reasoning", ""),
                    source=routing.get("source", "unknown"),
                    alt_mode=alt_mode,
                ),
                evidence=evidence,
                frontier_seeds=frontier,
                trace=trace,
                elapsed_ms=elapsed_ms,
                answer_prompt=result.get("answer_prompt", ""),
            )
        finally:
            if neo4j is not None:
                neo4j.close()
            if qdrant is not None:
                qdrant.close()
            if llm is not None:
                llm.close()
