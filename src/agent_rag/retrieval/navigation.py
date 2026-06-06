"""Mode B: Navigation Retrieval — cross-page information aggregation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import llm_config, thresholds_config
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

logger = structlog.get_logger(__name__)

_nav_cfg = thresholds_config.get("retrieval", {}).get("navigation", {})
DEFAULT_TOP_K_PAGES = int(_nav_cfg.get("top_k_pages", 3))
DEFAULT_TOP_K_BLOCKS = int(_nav_cfg.get("top_k_blocks", 3))
DEFAULT_TOP_BLOCKS_SELECTION = int(_nav_cfg.get("top_blocks_selection", 10))

_HYBRID_SPARSE_ENABLED = bool(
    thresholds_config.get("retrieval", {}).get("hybrid_sparse_enabled", False)
)
_BM25_TOP_K = int(thresholds_config.get("retrieval", {}).get("bm25_top_k", 20))

_GENERATION_MODEL = (llm_config.get("generation", {}) or {}).get("model")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))

_DECOMPOSE_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "query_decompose.j2")
    .read_text(encoding="utf-8")
)
_ANSWER_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_answer.j2")
    .read_text(encoding="utf-8")
)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _decompose_query(query: str, llm: LLMClient) -> list[dict[str, str]]:
    prompt = _DECOMPOSE_TMPL.render(query=query)
    with cost_stage("reasoning_extract"):
        raw = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
            use_cache=True,
        )
    try:
        data = json_repair.loads(raw)
        return data.get("sub_queries", [{"query": query, "focus": "general"}])
    except Exception:
        return [{"query": query, "focus": "general"}]


def _embed_text(text: str) -> list[float]:
    from agent_rag.retrieval._embedding import embed_query
    return embed_query(text)


def _search_pages_for_subquery(
    sub_query: str,
    sq_emb: list[float],
    qdrant: QdrantStore,
    neo4j: Neo4jStore,
    top_k_pages: int,
) -> tuple[list[str], set[str]]:
    """Run webpage ANN + LINKS_TO expansion once per sub-query.

    Returns (direct_urls, expanded_urls). ``expanded_urls`` is a superset that
    includes linked pages discovered via the LINKS_TO edge.
    """
    page_hits = qdrant.search("webpages", sq_emb, top_k=top_k_pages)
    direct_urls: list[str] = []
    for ph in page_hits:
        url = ph["payload"].get("url", "")
        if url:
            direct_urls.append(url)

    expanded_urls: set[str] = set(direct_urls)
    for url in direct_urls:
        linked = neo4j.get_linked_pages(url)
        for lp in linked:
            p = lp.get("page", {})
            if p.get("url"):
                expanded_urls.add(p["url"])
    return direct_urls, expanded_urls


def retrieve_navigation(
    query: str,
    query_embedding: list[float],
    neo4j: Neo4jStore,
    qdrant: QdrantStore,
    llm: LLMClient,
    top_k_pages: int | None = None,
    top_k_blocks: int | None = None,
    skip_answer: bool = False,
    history: list[Any] | None = None,
    answer_query: str | None = None,
) -> dict[str, Any]:
    """Mode B: query decomposition → WebPage ANN → CONTAINS → Block ANN → rerank.

    With the reranker enabled, the historic per-page Block top-k truncation is
    relaxed: each page contributes a wider candidate pool (``top_k_blocks``
    bumped to ``first_stage_top_k`` worth of slots) and the final ranking is
    decided by the cross-encoder across all pages together. This removes the
    blind spot where a high-quality block on a moderately-ranked page used to
    get cut by the per-page top-3 cap.
    """
    top_k_pages = top_k_pages if top_k_pages is not None else DEFAULT_TOP_K_PAGES
    top_k_blocks = top_k_blocks if top_k_blocks is not None else DEFAULT_TOP_K_BLOCKS

    rerank_on = reranker_is_enabled()
    # When reranking, ask each per-page Block ANN for a wider slice so the
    # reranker has variety to work with; cap to first_stage_top_k overall by
    # the holistic top_blocks cut below.
    if rerank_on:
        top_k_blocks = max(top_k_blocks, max(10, reranker_first_stage_top_k() // 5))
        holistic_cap = reranker_first_stage_top_k()
        final_cap = reranker_final_top_k()
    else:
        holistic_cap = DEFAULT_TOP_BLOCKS_SELECTION
        final_cap = DEFAULT_TOP_BLOCKS_SELECTION

    t0 = time.time()
    trace: list[dict[str, Any]] = []

    # Step 1: Query decomposition
    ts = time.perf_counter()
    sub_queries = _decompose_query(query, llm)
    trace.append({
        "step": "query_decompose",
        "label": "Query Decomposition",
        "duration_ms": _ms(ts),
        "data": {
            "sub_queries": [{"query": sq.get("query", ""), "focus": sq.get("focus", "")} for sq in sub_queries],
        },
    })

    # Pre-embed each unique sub-query once (avoid double embedding across phases).
    emb_cache: dict[str, list[float]] = {}
    page_cache: dict[str, tuple[list[str], set[str]]] = {}
    for sq in sub_queries:
        sq_text = sq.get("query", query)
        if sq_text not in emb_cache:
            emb_cache[sq_text] = _embed_text(sq_text)

    # Step 2: WebPage ANN search + LINKS_TO expansion (single pass)
    ts = time.perf_counter()
    pages_found = 0
    expanded_url_count = 0
    for sq in sub_queries:
        sq_text = sq.get("query", query)
        if sq_text in page_cache:
            continue
        direct_urls, expanded_urls = _search_pages_for_subquery(
            sq_text, emb_cache[sq_text], qdrant, neo4j, top_k_pages
        )
        page_cache[sq_text] = (direct_urls, expanded_urls)
        pages_found += len(direct_urls)
        expanded_url_count += len(expanded_urls)

    trace.append({
        "step": "webpage_search",
        "label": "WebPage Search + Link Expansion",
        "duration_ms": _ms(ts),
        "data": {"pages_found": pages_found, "pages_after_expansion": expanded_url_count},
    })

    # Step 3: Block retrieval per page (batch Neo4j + Qdrant search_batch)
    ts = time.perf_counter()
    all_blocks: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()

    # Pre-fetch all expanded URLs' blocks and webpage titles in two batch calls.
    all_expanded_urls: set[str] = set()
    for _, expanded_urls in page_cache.values():
        all_expanded_urls.update(expanded_urls)

    url_blocks_map = neo4j.get_blocks_for_webpages_batch(list(all_expanded_urls))
    url_webpage_map = neo4j.get_webpages_batch(list(all_expanded_urls))

    for sq in sub_queries:
        sq_text = sq.get("query", query)
        sq_emb = emb_cache[sq_text]
        _, expanded_urls = page_cache[sq_text]

        # Batch block searches for all URLs in this sub-query.
        url_list = list(expanded_urls)
        batch_requests = [
            {"collection": "blocks", "query": sq_emb, "top_k": top_k_blocks,
             "filters": {"url": url}}
            for url in url_list
        ]
        batch_hits = qdrant.search_batch(batch_requests) if url_list else []

        for url, block_hits in zip(url_list, batch_hits):
            page_blocks = url_blocks_map.get(url, [])
            if not page_blocks:
                continue

            webpage = url_webpage_map.get(url, {})
            source_title = webpage.get("title", "") if webpage else ""
            block_map = {b["block_id"]: b for b in page_blocks}

            if not block_hits:
                for b in page_blocks[:top_k_blocks]:
                    bid = b["block_id"]
                    if bid not in seen_block_ids:
                        seen_block_ids.add(bid)
                        all_blocks.append({
                            "block_id": bid,
                            "content": b.get("content", ""),
                            "heading_context": b.get("heading_context", ""),
                            "source_url": url,
                            "source_title": source_title,
                            "score": 0.5,
                            "sub_query": sq_text,
                        })
                continue

            for bh in block_hits:
                bid = bh["payload"].get("block_id", "")
                if bid in seen_block_ids:
                    continue
                seen_block_ids.add(bid)
                block = block_map.get(bid) or neo4j.get_block_by_id(bid) or {}
                all_blocks.append({
                    "block_id": bid,
                    "content": block.get("content", ""),
                    "heading_context": block.get("heading_context", ""),
                    "source_url": url,
                    "source_title": source_title,
                    "score": bh["score"],
                    "sub_query": sq_text,
                })

    all_blocks.sort(key=lambda x: x["score"], reverse=True)
    first_stage_blocks = all_blocks[:holistic_cap]

    # Hybrid sparse: union BM25 top-K into the first-stage pool. Mode B's
    # per-page Block ANN is constrained to the pages chosen by the WebPage
    # ANN — when the right page is not in WebPage top-K, the gold block is
    # unreachable via the dense path. BM25 over the global block corpus
    # bypasses that page-level gate. trusted-25 probe (2026-05-12) showed
    # +10pp gold-in-pool with zero regressions.
    bm25_added = 0
    if _HYBRID_SPARSE_ENABLED:
        seen_bids = {b["block_id"] for b in first_stage_blocks}
        for sparse_hit in _bm25.search(query, top_k=_BM25_TOP_K):
            bid = sparse_hit["payload"].get("block_id")
            if not bid or bid in seen_bids:
                continue
            block = neo4j.get_block_by_id(bid)
            if not block:
                continue
            webpage = neo4j.get_webpage_for_block(bid)
            first_stage_blocks.append({
                "block_id": bid,
                "content": block.get("content", ""),
                "heading_context": block.get("heading_context", ""),
                "source_url": (webpage.get("url") if webpage else "") or block.get("url", ""),
                "source_title": webpage.get("title", "") if webpage else "",
                "score": 0.0,
                "sub_query": query,
                "bm25_score": float(sparse_hit["score"]),
                "from_bm25": True,
            })
            seen_bids.add(bid)
            bm25_added += 1

    trace.append({
        "step": "block_retrieval",
        "label": "Block Retrieval (First-Stage)" + (" + BM25" if bm25_added else ""),
        "duration_ms": _ms(ts),
        "data": {
            "total_candidates": len(all_blocks),
            "first_stage_kept": len(first_stage_blocks),
            "first_stage_top_k": holistic_cap,
            "bm25_added": bm25_added,
        },
    })

    # Query rewrites — fan rewrites through dense + BM25 over the global
    # block corpus. Especially useful for mode_b because the per-page Block
    # ANN is constrained to pages chosen by WebPage ANN; rewrites can pull
    # blocks from pages the original query never touched.
    rewrites = _rewriter.rewrite_query(query)
    if rewrites:
        ts_rw = time.perf_counter()
        first_stage_blocks, rw_added = expand_pool_with_rewrites(
            rewrites=rewrites,
            existing_pool=first_stage_blocks,
            qdrant=qdrant,
            neo4j=neo4j,
            extra_fields={"sub_query": query},
        )
        trace.append({
            "step": "query_rewrite",
            "label": "Query Rewrite Union",
            "duration_ms": _ms(ts_rw),
            "data": {"rewrites": rewrites, "added": rw_added},
        })

    # Second-stage rerank across all pages together.
    if rerank_on and first_stage_blocks:
        ts = time.perf_counter()
        rerank_n = (
            min(len(first_stage_blocks), max(final_cap * 5, 50))
            if mmr_is_enabled()
            else final_cap
        )
        reranked = rerank_blocks(
            query=query,
            blocks=first_stage_blocks,
            top_n=rerank_n,
        )
        if mmr_is_enabled() and len(reranked) > final_cap:
            top_blocks = mmr_select(reranked, final_k=final_cap)
        else:
            top_blocks = reranked[:final_cap]
        first_rerank_score = top_blocks[0].get("rerank_score") if top_blocks else None
        trace.append({
            "step": "rerank",
            "label": "Cross-Encoder Rerank",
            "duration_ms": _ms(ts),
            "data": {
                "candidates": len(first_stage_blocks),
                "rerank_pool": len(reranked),
                "kept": len(top_blocks),
                "top_score": first_rerank_score,
                "mmr_lambda": mmr_lambda() if mmr_is_enabled() else None,
            },
        })
    else:
        top_blocks = first_stage_blocks[:final_cap]

    # Step 4: Answer generation
    ts = time.perf_counter()
    related_links = collect_related_links(top_blocks, neo4j)
    prompt = _ANSWER_TMPL.render(
        query=answer_query or query,
        blocks=top_blocks,
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
        "mode": "B",
        "sub_queries": sub_queries,
        "blocks": top_blocks,
        "answer_prompt": prompt,
        "elapsed_seconds": round(elapsed, 2),
        "trace": trace,
    }
