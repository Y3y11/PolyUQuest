"""Mode A: Direct Retrieval — single-hop factual questions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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

_HYBRID_SPARSE_ENABLED = bool(
    thresholds_config.get("retrieval", {}).get("hybrid_sparse_enabled", False)
)
_BM25_TOP_K = int(thresholds_config.get("retrieval", {}).get("bm25_top_k", 20))

logger = structlog.get_logger(__name__)

_GENERATION_MODEL = (llm_config.get("generation", {}) or {}).get("model")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))

_ANSWER_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_answer.j2")
    .read_text(encoding="utf-8")
)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def retrieve_direct(
    query: str,
    query_embedding: list[float],
    neo4j: Neo4jStore,
    qdrant: QdrantStore,
    llm: LLMClient,
    top_k: int | None = None,
    skip_answer: bool = False,
    history: list[Any] | None = None,
    answer_query: str | None = None,
) -> dict[str, Any]:
    """Mode A: Block ANN → reranker → PARENT_BLOCK heading context → LLM answer."""
    t0 = time.time()
    trace: list[dict[str, Any]] = []

    # Two-stage retrieval: first_stage_top_k → cross-encoder rerank → final_top_k.
    # When the caller pins top_k (e.g. an evaluation harness asks for a different
    # final cap), we honor it for the final size only; first-stage still pulls a
    # wide pool so the reranker has enough candidates to discriminate.
    if reranker_is_enabled():
        first_stage_k = reranker_first_stage_top_k()
        effective_top_k = top_k if top_k is not None else reranker_final_top_k()
    else:
        first_stage_k = top_k if top_k is not None else int(
            thresholds_config.get("retrieval", {}).get("direct_top_k", 5)
        )
        effective_top_k = first_stage_k

    # Step 1: Block ANN search (first-stage). When hybrid sparse is enabled,
    # union the dense top-K pool with BM25 top-bm25_top_k on the same block
    # corpus. BM25 lights up exact term overlap (dept acronyms, proper-noun
    # event names, date strings) that BGE-M3 occasionally buries under
    # semantically similar but topically wrong neighbours.
    ts = time.perf_counter()
    hits = qdrant.search("blocks", query_embedding, top_k=first_stage_k)
    bm25_added = 0
    if _HYBRID_SPARSE_ENABLED:
        seen_ids = {h["payload"].get("block_id") for h in hits}
        for sparse_hit in _bm25.search(query, top_k=_BM25_TOP_K):
            bid = sparse_hit["payload"].get("block_id")
            if not bid or bid in seen_ids:
                continue
            # BM25 scores are not on the [0,1] cosine scale. Mark the entry so
            # downstream (reranker fusion) doesn't accidentally mix scales —
            # for pure-rerank (α=1.0) this field is unused.
            hits.append({
                "payload": sparse_hit["payload"],
                "score": 0.0,
                "bm25_score": sparse_hit["score"],
            })
            seen_ids.add(bid)
            bm25_added += 1
    trace.append({
        "step": "block_search",
        "label": "Block ANN Search" + (" + BM25" if _HYBRID_SPARSE_ENABLED else ""),
        "duration_ms": _ms(ts),
        "data": {
            "first_stage_top_k": first_stage_k,
            "hits_count": len(hits),
            "bm25_added": bm25_added,
        },
    })

    # Step 2: Context enrichment for first-stage candidates
    ts = time.perf_counter()
    candidates: list[dict[str, Any]] = []
    for hit in hits:
        block_id = hit["payload"].get("block_id", "")
        block = neo4j.get_block_by_id(block_id)
        if not block:
            continue

        heading = block.get("heading_context", "")

        webpage = neo4j.get_webpage_for_block(block_id)
        source_url = webpage.get("url", "") if webpage else ""
        source_title = webpage.get("title", "") if webpage else ""

        candidates.append({
            "block_id": block_id,
            "content": block.get("content", ""),
            "heading_context": heading,
            "source_url": source_url,
            "source_title": source_title,
            "score": hit["score"],
        })
    trace.append({
        "step": "context_enrichment",
        "label": "Context Enrichment",
        "duration_ms": _ms(ts),
        "data": {"blocks_enriched": len(candidates)},
    })

    # Query rewrites: fan up to N rewrites through dense + BM25 and union new
    # blocks into the rerank input. Targets multilingual + paraphrase gaps
    # where the original literal English query has zero overlap with the gold
    # passage (zh-only pages, "language competitions" vs "Korean Speech
    # Contest", etc.). Reranker decides the final ranking.
    rewrites = _rewriter.rewrite_query(query)
    if rewrites:
        ts = time.perf_counter()
        candidates, rw_added = expand_pool_with_rewrites(
            rewrites=rewrites,
            existing_pool=candidates,
            qdrant=qdrant,
            neo4j=neo4j,
        )
        trace.append({
            "step": "query_rewrite",
            "label": "Query Rewrite Union",
            "duration_ms": _ms(ts),
            "data": {"rewrites": rewrites, "added": rw_added},
        })

    # Step 2b: Second-stage rerank
    if reranker_is_enabled() and candidates:
        ts = time.perf_counter()
        # When MMR is on, rerank a wider pool (≥5×final_k) so MMR has room to
        # trade relevance for diversity. Otherwise keep the legacy top_n=final_k.
        rerank_n = (
            min(len(candidates), max(effective_top_k * 5, 50))
            if mmr_is_enabled()
            else effective_top_k
        )
        reranked = rerank_blocks(
            query=query,
            blocks=candidates,
            top_n=rerank_n,
        )
        if mmr_is_enabled() and len(reranked) > effective_top_k:
            blocks_with_context = mmr_select(reranked, final_k=effective_top_k)
        else:
            blocks_with_context = reranked[:effective_top_k]
        top_score = (
            blocks_with_context[0].get("rerank_score") if blocks_with_context else None
        )
        trace.append({
            "step": "rerank",
            "label": "Cross-Encoder Rerank",
            "duration_ms": _ms(ts),
            "data": {
                "candidates": len(candidates),
                "rerank_pool": len(reranked),
                "kept": len(blocks_with_context),
                "top_score": top_score,
                "mmr_lambda": mmr_lambda() if mmr_is_enabled() else None,
            },
        })
    else:
        blocks_with_context = candidates[:effective_top_k]

    # Step 3: Answer generation
    ts = time.perf_counter()
    related_links = collect_related_links(blocks_with_context, neo4j)
    prompt = _ANSWER_TMPL.render(
        query=answer_query or query,
        blocks=blocks_with_context,
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
        "mode": "A",
        "blocks": blocks_with_context,
        "answer_prompt": prompt,
        "elapsed_seconds": round(elapsed, 2),
        "trace": trace,
    }
