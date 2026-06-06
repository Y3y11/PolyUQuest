"""Shared first-stage pool expansion for query rewrites.

Mode A / B / C all need the same operation: take an existing candidate pool
of blocks, fan out each rewrite query through (dense_top_K, BM25_top_K),
union-dedup new block ids, look up Block + WebPage in Neo4j, and append to
the pool with rewrite-source diagnostics. Keeping this in one place avoids
three copies of the same Neo4j N+1 loop.

The reranker is the only judge of relevance — rewrite scores are NOT mixed
into any arithmetic. We mark each rewrite-sourced block with ``from_rewrite``
so downstream eval can attribute the lift.
"""
from __future__ import annotations

from typing import Any

import structlog

from agent_rag.config import thresholds_config
from agent_rag.retrieval import _bm25
from agent_rag.retrieval._embedding import embed_query
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)

_cfg = thresholds_config.get("retrieval", {}).get("query_rewrite", {}) or {}
_DENSE_TOP_K = int(_cfg.get("per_rewrite_dense_top_k", 20))
_BM25_TOP_K = int(_cfg.get("per_rewrite_bm25_top_k", 10))


def _enrich_block(bid: str, neo4j: Neo4jStore) -> dict[str, Any] | None:
    """Fetch block + parent webpage for a fresh BID. Returns None if missing."""
    block = neo4j.get_block_by_id(bid)
    if not block:
        return None
    webpage = neo4j.get_webpage_for_block(bid)
    return {
        "block_id": bid,
        "content": block.get("content", ""),
        "heading_context": block.get("heading_context", ""),
        "source_url": (webpage.get("url") if webpage else "") or block.get("url", ""),
        "source_title": webpage.get("title", "") if webpage else "",
        "token_count": block.get("token_count", 100),
    }


def expand_pool_with_rewrites(
    rewrites: list[str],
    existing_pool: list[dict[str, Any]],
    qdrant: QdrantStore,
    neo4j: Neo4jStore,
    extra_fields: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fan each rewrite through dense + BM25, union new blocks into the pool.

    Args:
        rewrites: list of rewrite strings (caller already excludes the original).
        existing_pool: blocks already collected by the mode's primary path.
            Each entry must have ``block_id``. Other fields are preserved as-is.
        qdrant: open Qdrant store (caller manages lifecycle).
        neo4j: open Neo4j store (caller manages lifecycle).
        extra_fields: optional dict of mode-specific defaults to merge into
            each rewrite-sourced entry (e.g. ``{"score": 0.0,
            "heuristic_score": 0.0, "entity_coverage": 0}`` for mode_c).

    Returns:
        (expanded_pool, num_added). ``expanded_pool`` is a new list — input is
        not mutated.
    """
    if not rewrites:
        return list(existing_pool), 0

    seen_bids: set[str] = {b.get("block_id") for b in existing_pool if b.get("block_id")}
    out = list(existing_pool)
    added = 0

    for rw in rewrites:
        rw = rw.strip()
        if not rw:
            continue
        # Dense leg
        try:
            rw_emb = embed_query(rw, expand=True)
            dense_hits = qdrant.search("blocks", rw_emb, top_k=_DENSE_TOP_K)
        except Exception as exc:
            logger.warning("rewrite_dense_error", rewrite=rw[:80], error=str(exc))
            dense_hits = []
        # BM25 leg
        try:
            bm25_hits = _bm25.search(rw, top_k=_BM25_TOP_K)
        except Exception as exc:
            logger.warning("rewrite_bm25_error", rewrite=rw[:80], error=str(exc))
            bm25_hits = []

        merged_bids: list[tuple[str, float, float]] = []
        for h in dense_hits:
            bid = h["payload"].get("block_id")
            if bid:
                merged_bids.append((bid, float(h.get("score", 0.0)), 0.0))
        for h in bm25_hits:
            bid = h["payload"].get("block_id")
            if bid:
                merged_bids.append((bid, 0.0, float(h.get("score", 0.0))))

        for bid, dense_score, bm25_score in merged_bids:
            if bid in seen_bids:
                continue
            row = _enrich_block(bid, neo4j)
            if row is None:
                continue
            row["score"] = 0.0  # not on cosine scale; reranker decides
            if dense_score:
                row["rewrite_dense_score"] = dense_score
            if bm25_score:
                row["rewrite_bm25_score"] = bm25_score
            row["from_rewrite"] = rw
            if extra_fields:
                for k, v in extra_fields.items():
                    row.setdefault(k, v)
            out.append(row)
            seen_bids.add(bid)
            added += 1

    return out, added
