"""Mode C: Reasoning Retrieval — multi-hop entity reasoning with normalized scoring."""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import llm_config, stage_model, thresholds_config
from agent_rag.llm.client import LLMClient, cost_stage
from agent_rag.retrieval import _bm25, _rewriter
from agent_rag.retrieval._context import collect_related_links
from agent_rag.retrieval._rewrite_pool import expand_pool_with_rewrites
from agent_rag.retrieval._reranker import (
    final_top_k as reranker_final_top_k,
)
from agent_rag.retrieval._reranker import (
    first_stage_top_k as reranker_first_stage_top_k,
)
from agent_rag.retrieval._reranker import (
    is_enabled as reranker_is_enabled,
)
from agent_rag.retrieval._reranker import (
    rerank_blocks,
)
from agent_rag.retrieval._mmr import mmr_is_enabled, mmr_lambda, mmr_select
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore

# Ablation switch: when True, skip the TopicKeyword (high-level keyword → relation
# ANN) leg of mode_c and rely only on entity ANN + graph traversal. Used by the
# `no_topic_keywords` eval variant to isolate the TopicKeyword contribution.
_topic_keywords_disabled: ContextVar[bool] = ContextVar(
    "topic_keywords_disabled", default=False
)


def set_topic_keywords_disabled(value: bool) -> Token[bool]:
    return _topic_keywords_disabled.set(value)


def reset_topic_keywords_disabled(token: Token[bool]) -> None:
    _topic_keywords_disabled.reset(token)

_HYBRID_SPARSE_ENABLED = bool(
    thresholds_config.get("retrieval", {}).get("hybrid_sparse_enabled", False)
)
_BM25_TOP_K = int(thresholds_config.get("retrieval", {}).get("bm25_top_k", 20))

logger = structlog.get_logger(__name__)

_scoring = thresholds_config.get("scoring", {})
ALPHA = _scoring.get("alpha", 0.5)
BETA = _scoring.get("beta", 0.35)
GAMMA = _scoring.get("gamma", 0.15)
COVERAGE_CAP = _scoring.get("coverage_cap", 5)
MAX_TOKENS = _scoring.get("max_tokens", 300)
SAME_PAGE_CAP = _scoring.get("same_page_cap", 3)
SAME_PAGE_DISCOUNT = _scoring.get("same_page_discount", 0.5)
CONTEXT_BUDGET = _scoring.get("context_budget", 4000)
TOP_ENTITY_LIMIT = int(_scoring.get("top_entity_limit", 30))

_GENERATION_MODEL = stage_model("generation")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))

_KW_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "extract_keywords.j2")
    .read_text(encoding="utf-8")
)
_ANSWER_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_answer.j2")
    .read_text(encoding="utf-8")
)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _extract_keywords(query: str, llm: LLMClient) -> list[str]:
    prompt = _KW_TMPL.render(query=query)
    with cost_stage("reasoning_extract"):
        raw = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
            use_cache=True,
        )
    try:
        data = json_repair.loads(raw)
        return data.get("keywords", [])
    except Exception:
        return []


def _embed_text(text: str) -> list[float]:
    from agent_rag.retrieval._embedding import embed_query
    return embed_query(text)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    from numpy import dot
    from numpy.linalg import norm
    na, nb = norm(a), norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(dot(a, b) / (na * nb))


def _relevance_score(
    query_emb: list[float],
    block_emb: list[float],
    entity_coverage: int,
    token_count: int,
) -> float:
    cos = _cosine_sim(query_emb, block_emb)
    cov = min(entity_coverage, COVERAGE_CAP) / COVERAGE_CAP
    eff = max(0.0, 1.0 - token_count / MAX_TOKENS)
    return ALPHA * cos + BETA * cov + GAMMA * eff


def retrieve_reasoning(
    query: str,
    query_embedding: list[float],
    neo4j: Neo4jStore,
    qdrant: QdrantStore,
    llm: LLMClient,
    top_n_entities: int = 10,
    top_m_neighbors: int = 10,
    max_hops: int = 2,
    skip_answer: bool = False,
    history: list[Any] | None = None,
    answer_query: str | None = None,
) -> dict[str, Any]:
    """Mode C: Entity ANN + TopicKeyword → graph traversal → scored Block selection."""
    t0 = time.time()
    trace: list[dict[str, Any]] = []

    # Step 1: Low-level entity retrieval
    ts = time.perf_counter()
    entity_hits = qdrant.search("entities", query_embedding, top_k=top_n_entities)
    e_local = {h["payload"].get("entity_id", ""): h for h in entity_hits if h["payload"].get("entity_id")}
    top_entities = [
        {"name": h["payload"].get("entity_name", ""), "score": round(h["score"], 3)}
        for h in entity_hits[:5]
        if h["payload"].get("entity_name")
    ]
    trace.append({
        "step": "entity_search",
        "label": "Entity ANN Search",
        "duration_ms": _ms(ts),
        "data": {"hits": len(e_local), "top_entities": top_entities},
    })

    # Step 2: High-level topic keyword retrieval (batched relation search).
    # The ablation switch short-circuits before the LLM keyword-extraction call
    # so we measure both retrieval-quality impact *and* the LLM-call savings
    # in a single variant.
    ts = time.perf_counter()
    topic_kw_off = _topic_keywords_disabled.get()
    keywords: list[str] = []
    r_global_entities: set[str] = set()
    if not topic_kw_off:
        keywords = _extract_keywords(query, llm)
        if keywords:
            kw_embs = [_embed_text(kw) for kw in keywords]
            rel_results = qdrant.search_batch([
                {"collection": "relations", "query": emb, "top_k": 5}
                for emb in kw_embs
            ])
            for hits in rel_results:
                for rh in hits:
                    for key in ("source_entity", "target_entity"):
                        eid = rh["payload"].get(key, "")
                        if eid:
                            r_global_entities.add(eid)
    trace.append({
        "step": "keyword_extraction",
        "label": "Keyword Extraction & Relation Search",
        "duration_ms": _ms(ts),
        "data": {
            "keywords": keywords,
            "topic_entities_found": len(r_global_entities),
            "disabled": topic_kw_off,
        },
    })

    # Step 3: Graph traversal expansion (batched)
    ts = time.perf_counter()
    initial_count = len(set(e_local.keys()) | r_global_entities)
    e_expanded: set[str] = set(e_local.keys()) | r_global_entities
    # Track best path_weight per neighbor (a neighbor reachable from multiple
    # seeds gets credit for its strongest path).
    neighbor_weight: dict[str, float] = {}
    neighbor_map = neo4j.get_entity_neighbors_batch(
        list(e_local.keys()), max_hops=max_hops, top_m=top_m_neighbors
    )
    for _eid, neighbors in neighbor_map.items():
        for nb in neighbors:
            nbid = nb.get("entity_id", "")
            if not nbid:
                continue
            e_expanded.add(nbid)
            w = float(nb.get("weight", 0.0) or 0.0)
            if w > neighbor_weight.get(nbid, 0.0):
                neighbor_weight[nbid] = w
    trace.append({
        "step": "graph_traversal",
        "label": "Graph Traversal (RELATES_TO)",
        "duration_ms": _ms(ts),
        "data": {
            "initial_entities": initial_count,
            "expanded_entities": len(e_expanded),
            "hops": max_hops,
            "neo4j_calls": 1,
        },
    })

    # Entity pruning with per-class quotas (Beam-style):
    #   seed   (e_local, ANN-scored)        -> guaranteed slots
    #   global (relation-derived)           -> guaranteed slots
    #   neighbor (graph-traversed)          -> ranked by path_weight
    # This prevents a flood of relation/global entities from crowding out
    # multi-hop neighbors, preserving Mode C's reasoning value.
    seed_entities = list(e_local.keys())
    global_only = [eid for eid in r_global_entities if eid not in e_local]
    neighbor_only = [eid for eid in e_expanded if eid not in e_local and eid not in r_global_entities]

    seed_quota = min(len(seed_entities), max(10, TOP_ENTITY_LIMIT // 3))
    global_quota = min(len(global_only), max(5, TOP_ENTITY_LIMIT // 3))

    # Stable tie-breakers: when scores collide, fall back to entity_id so the
    # pruning result is reproducible across runs (set/dict iteration order is
    # otherwise hash-dependent).
    seed_sorted = sorted(
        seed_entities,
        key=lambda x: (-e_local[x]["score"], x),
    )[:seed_quota]
    global_sorted = sorted(global_only)[:global_quota]
    neighbor_sorted = sorted(
        neighbor_only,
        key=lambda x: (-neighbor_weight.get(x, 0.0), x),
    )
    remaining = max(0, TOP_ENTITY_LIMIT - len(seed_sorted) - len(global_sorted))
    neighbor_kept = neighbor_sorted[:remaining]

    e_pruned: set[str] = set(seed_sorted) | set(global_sorted) | set(neighbor_kept)

    # Step 4: Retrieve blocks (batch) and score (one vector retrieve + local cosine)
    ts = time.perf_counter()
    candidate_blocks: dict[str, dict[str, Any]] = {}
    block_entity_coverage: dict[str, set[str]] = {}

    entity_blocks_map = neo4j.get_blocks_for_entities_batch(list(e_pruned))
    for eid, blocks in entity_blocks_map.items():
        for b in blocks:
            bid = b.get("block_id", "")
            if not bid:
                continue
            if bid not in candidate_blocks:
                candidate_blocks[bid] = b
                block_entity_coverage[bid] = set()
            block_entity_coverage[bid].add(eid)

    # Fetch all candidate block vectors in one HTTP call, then compute cosine
    # locally. Equivalent to per-block filtered ANN (top_k=1) but O(1) requests
    # and preserves true cosine for *every* graph candidate (not just those in
    # a global top-N window).
    candidate_bids_list = list(candidate_blocks.keys())
    block_vec_map = qdrant.retrieve_vectors("blocks", candidate_bids_list)
    block_score_map: dict[str, float] = {}
    if block_vec_map:
        import numpy as np
        q_arr = np.asarray(query_embedding, dtype=np.float32)
        q_norm = float(np.linalg.norm(q_arr)) or 1.0
        for bid, vec in block_vec_map.items():
            v = np.asarray(vec, dtype=np.float32)
            vn = float(np.linalg.norm(v))
            if vn == 0.0:
                continue
            block_score_map[bid] = float(np.dot(q_arr, v) / (q_norm * vn))

    scored: list[tuple[str, float, dict[str, Any]]] = []
    for bid, block in candidate_blocks.items():
        cos_score = block_score_map.get(bid, 0.0)
        coverage = len(block_entity_coverage.get(bid, set()))
        tc = block.get("token_count", 100)
        cov_norm = min(coverage, COVERAGE_CAP) / COVERAGE_CAP
        eff_norm = max(0.0, 1.0 - tc / MAX_TOKENS)
        score = ALPHA * cos_score + BETA * cov_norm + GAMMA * eff_norm
        scored.append((bid, score, block))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Pre-fetch all webpages in one batch before the selection loop.
    candidate_bids = [bid for bid, _, _ in scored]
    webpage_map = neo4j.get_webpages_for_blocks_batch(candidate_bids)

    # First-stage candidate cap: graph traversal can fan out to hundreds of
    # blocks. Restrict the rerank input to the top-N by the α/β/γ heuristic so
    # the cross-encoder call stays bounded; the heuristic is reasonable as a
    # coarse filter (it captures cosine + entity coverage + length), the
    # reranker decides the final ordering.
    rerank_on = reranker_is_enabled()
    first_stage_cap = reranker_first_stage_top_k() if rerank_on else len(scored)
    first_stage_pool = scored[:first_stage_cap]

    pool_blocks: list[dict[str, Any]] = []
    for bid, heuristic_score, block in first_stage_pool:
        webpage = webpage_map.get(bid)
        canonical_url = (webpage.get("url") if webpage else "") or block.get("url", "")
        pool_blocks.append({
            "block_id": bid,
            "content": block.get("content", ""),
            "heading_context": block.get("heading_context", ""),
            "source_url": canonical_url,
            "source_title": webpage.get("title", "") if webpage else "",
            "score": heuristic_score,
            "heuristic_score": heuristic_score,
            "entity_coverage": len(block_entity_coverage.get(bid, set())),
            "token_count": block.get("token_count", 100),
        })

    # Hybrid sparse: union BM25 top-K into the rerank input pool. Mode C's
    # entity-graph traversal is precise but recall-bound — it can miss blocks
    # whose gold terms (proper nouns, dept acronyms, dates) are not anchored
    # by any extracted entity. BM25 catches those cheaply. The reranker still
    # has final say. trusted-25 probe (2026-05-12): same +10pp gold-in-pool
    # holds when union happens before the cross-encoder.
    bm25_added = 0
    if _HYBRID_SPARSE_ENABLED:
        seen_bids = {b["block_id"] for b in pool_blocks}
        for sparse_hit in _bm25.search(query, top_k=_BM25_TOP_K):
            bid = sparse_hit["payload"].get("block_id")
            if not bid or bid in seen_bids:
                continue
            block = neo4j.get_block_by_id(bid)
            if not block:
                continue
            webpage = neo4j.get_webpage_for_block(bid)
            pool_blocks.append({
                "block_id": bid,
                "content": block.get("content", ""),
                "heading_context": block.get("heading_context", ""),
                "source_url": (webpage.get("url") if webpage else "") or block.get("url", ""),
                "source_title": webpage.get("title", "") if webpage else "",
                "score": 0.0,
                "heuristic_score": 0.0,
                "entity_coverage": 0,
                "token_count": block.get("token_count", 100),
                "bm25_score": float(sparse_hit["score"]),
                "from_bm25": True,
            })
            seen_bids.add(bid)
            bm25_added += 1
        if bm25_added:
            trace.append({
                "step": "bm25_union",
                "label": "BM25 Sparse Union",
                "duration_ms": 0,
                "data": {"bm25_top_k": _BM25_TOP_K, "added": bm25_added},
            })

    # Query rewrites — same union pattern, after BM25 so dedup catches both.
    # Mode C's entity-graph traversal is anchored to entities extracted from
    # the corpus; if those anchors live behind a language or paraphrase gap,
    # neither dense nor BM25 over the original query will find them.
    rewrites = _rewriter.rewrite_query(query)
    if rewrites:
        ts_rw = time.perf_counter()
        pool_blocks, rw_added = expand_pool_with_rewrites(
            rewrites=rewrites,
            existing_pool=pool_blocks,
            qdrant=qdrant,
            neo4j=neo4j,
            extra_fields={"heuristic_score": 0.0, "entity_coverage": 0},
        )
        trace.append({
            "step": "query_rewrite",
            "label": "Query Rewrite Union",
            "duration_ms": _ms(ts_rw),
            "data": {"rewrites": rewrites, "added": rw_added},
        })

    if rerank_on and pool_blocks:
        ts_rr = time.perf_counter()
        reranked = rerank_blocks(
            query=query,
            blocks=pool_blocks,
            # Rerank a generous slice to give the budget loop room to apply
            # context-budget + same-page-cap on a real ranking.
            top_n=min(len(pool_blocks), max(reranker_final_top_k() * 3, 30)),
        )
        first_rerank_score = reranked[0].get("rerank_score") if reranked else None
        trace.append({
            "step": "rerank",
            "label": "Cross-Encoder Rerank",
            "duration_ms": _ms(ts_rr),
            "data": {
                "candidates": len(pool_blocks),
                "kept": len(reranked),
                "top_score": first_rerank_score,
            },
        })
        ts = time.perf_counter()
    else:
        reranked = pool_blocks

    final_cap = reranker_final_top_k() if rerank_on else len(reranked)
    # When MMR is on, re-order `reranked` by MMR so the token-budget loop below
    # picks a diversity-aware top-k. We only reorder the slice that the loop
    # could possibly fill; everything past final_cap is dropped anyway.
    if rerank_on and mmr_is_enabled() and len(reranked) > final_cap:
        mmr_head = mmr_select(reranked, final_k=final_cap)
        seen_ids = {b.get("block_id") for b in mmr_head}
        tail = [b for b in reranked if b.get("block_id") not in seen_ids]
        reranked = mmr_head + tail
    selected: list[dict[str, Any]] = []
    total_tokens = 0
    page_counts: dict[str, int] = {}

    for entry in reranked:
        if total_tokens >= CONTEXT_BUDGET or len(selected) >= final_cap:
            break
        canonical_url = entry.get("source_url", "")
        pc = page_counts.get(canonical_url, 0)
        primary_score = entry.get("rerank_score")
        if primary_score is None:
            primary_score = entry.get("heuristic_score", 0.0)
        adjusted_score = float(primary_score) * (
            SAME_PAGE_DISCOUNT if pc >= SAME_PAGE_CAP else 1.0
        )

        tc = entry.get("token_count", 100)
        # Build the output row from the enriched entry so we keep the rerank
        # diagnostics (first_stage_rank, heuristic_score) for downstream eval.
        row = dict(entry)
        row["score"] = round(adjusted_score, 4)
        row.pop("token_count", None)
        selected.append(row)
        total_tokens += tc
        page_counts[canonical_url] = pc + 1

    score_range = [round(scored[0][1], 3), round(scored[-1][1], 3)] if scored else [0, 0]
    trace.append({
        "step": "block_scoring",
        "label": "Block Scoring & Selection",
        "duration_ms": _ms(ts),
        "data": {
            "candidates": len(candidate_blocks),
            "first_stage_pool": len(pool_blocks),
            "selected": len(selected),
            "heuristic_score_range": score_range,
        },
    })

    # Step 5: Generate answer
    ts = time.perf_counter()
    related_links = collect_related_links(selected, neo4j)
    prompt = _ANSWER_TMPL.render(
        query=answer_query or query,
        blocks=selected,
        related_links=related_links,
        history=history,
    )
    chat_kwargs: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": _GENERATION_MAX_TOKENS,
        "use_cache": True,
    }
    if _GENERATION_MODEL:
        chat_kwargs["model"] = _GENERATION_MODEL
    if skip_answer:
        answer = ""
    else:
        with cost_stage("answer"):
            answer = llm.chat(**chat_kwargs)
    trace.append({
        "step": "answer_generation",
        "label": "Answer Generation",
        "duration_ms": _ms(ts),
        "data": {
            "prompt_tokens_est": len(prompt.split()),
            "skipped": skip_answer,
        },
    })

    elapsed = time.time() - t0
    return {
        "answer": answer,
        "mode": "C",
        "blocks": selected,
        "entities_expanded": len(e_expanded),
        "entities_after_pruning": len(e_pruned),
        "keywords_extracted": keywords,
        "answer_prompt": prompt,
        "elapsed_seconds": round(elapsed, 2),
        "trace": trace,
    }
