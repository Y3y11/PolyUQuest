"""Mode D: Subgraph Retrieval — entity + relation + supporting blocks as the unit.

Anchor entities (entity ANN) → k-hop neighbors via RELATES_TO → bounded V →
internal RELATES_TO edges E → EXTRACTED_FROM supporting blocks B → score blocks
with mode_c's α/β/γ heuristic → optional cross-encoder rerank → render a new
prompt that exposes the (V, E, B) subgraph to the LLM. Citations stay on
blocks; entities/relations are background context.

Designed as a sibling of ``reasoning.py``, not a refactor — the two retrievers
must be runnable side-by-side in evaluate.py for the paired comparison
(``mode_c_forced`` vs ``mode_d_no_rerank`` vs ``mode_d_forced``) that isolates
whether "subgraph as generation context" gives any uplift on PolyU.

The TopicKeyword leg of mode_c is intentionally omitted in v1 — see plan file
``delegated-cuddling-rivest.md`` for rationale.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import llm_config, stage_model, thresholds_config
from agent_rag.llm.client import LLMClient, cost_stage
from agent_rag.retrieval._context import collect_related_links
from agent_rag.retrieval._embedding import embed_query
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
from agent_rag.retrieval._mmr import mmr_is_enabled, mmr_select
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.storage.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)

# Reuse mode_c's scoring constants verbatim so mode_d's first-stage block
# ordering is comparable to mode_c. Changing these would confound "subgraph
# in prompt" with "different block scoring".
_scoring = thresholds_config.get("scoring", {})
ALPHA = _scoring.get("alpha", 0.5)
BETA = _scoring.get("beta", 0.35)
GAMMA = _scoring.get("gamma", 0.15)
COVERAGE_CAP = _scoring.get("coverage_cap", 5)
MAX_TOKENS = _scoring.get("max_tokens", 300)
SAME_PAGE_CAP = _scoring.get("same_page_cap", 3)
SAME_PAGE_DISCOUNT = _scoring.get("same_page_discount", 0.5)
CONTEXT_BUDGET = _scoring.get("context_budget", 4000)

# Mode-D-specific knobs. Anchor entity_cap mirrors mode_c's TOP_ENTITY_LIMIT;
# the rest are new bounds for the subgraph itself.
ENTITY_CAP = int(_scoring.get("subgraph_entity_cap", 25))
EDGE_CAP = int(_scoring.get("subgraph_edge_cap", 40))
SUBGRAPH_DESC_CHARS = int(_scoring.get("subgraph_desc_chars", 140))
SUBGRAPH_REL_DESC_CHARS = int(_scoring.get("subgraph_rel_desc_chars", 120))

_GENERATION_MODEL = stage_model("generation")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))

_ANSWER_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_answer_subgraph.j2")
    .read_text(encoding="utf-8")
)
# Same template mode_c uses for query → high-level keywords. Loaded here so
# subgraph.py stays independently runnable (and the keyword leg doesn't depend
# on importing private helpers from reasoning.py).
_KW_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "extract_keywords.j2")
    .read_text(encoding="utf-8")
)


def _extract_keywords(query: str, llm: LLMClient) -> list[str]:
    """Inline copy of reasoning._extract_keywords — identical contract so the
    matched-comparison variant (mode_d_topic_*) hits the same LLM cache."""
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


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _estimate_tokens(text: str) -> int:
    """Cheap token estimator: 1 token ≈ 0.75 words for English, 1 char for CJK.

    We don't need exactness here — only enough fidelity to keep the
    subgraph prompt segment from cannibalizing the block budget (plan
    risk #3). Block selection's existing ``token_count`` field is the
    source of truth for block sizes; this function is only used for the
    rough subgraph-side accounting.
    """
    if not text:
        return 0
    # Approximate: count words + half the CJK character runs.
    n_words = max(1, len(text.split()))
    return int(n_words * 1.3)


def _estimate_subgraph_tokens(
    entity_rows: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> int:
    """Estimate how many tokens the rendered Knowledge Subgraph section costs.

    Mirrors the per-line format used by ``generate_answer_subgraph.j2``:
    ``- {name} ({type}): {description[:DESC]}`` for entities and
    ``- A —[type, w=…]→ B: {description[:REL_DESC]}`` for relations.
    """
    total = 60  # section headers / preamble
    for e in entity_rows:
        name = e.get("entity_name") or ""
        etype = e.get("entity_type") or ""
        desc = (e.get("description") or "")[:SUBGRAPH_DESC_CHARS]
        total += _estimate_tokens(f"- {name} ({etype}): {desc}")
    for r in edges:
        line = (
            f"- {r.get('source_name', '')} —[{r.get('relation_type', '')}, "
            f"w={float(r.get('weight') or 0.0):.2f}]→ {r.get('target_name', '')}"
        )
        if r.get("description"):
            line += f": {str(r['description'])[:SUBGRAPH_REL_DESC_CHARS]}"
        total += _estimate_tokens(line)
    return total


def retrieve_subgraph(
    query: str,
    query_embedding: list[float],
    neo4j: Neo4jStore,
    qdrant: QdrantStore,
    llm: LLMClient,
    top_n_entities: int = 10,
    top_m_neighbors: int = 8,
    max_hops: int = 2,
    entity_cap: int = ENTITY_CAP,
    edge_cap: int = EDGE_CAP,
    use_reranker: bool = True,
    use_topic_keywords: bool = False,
    skip_answer: bool = False,
    history: list[Any] | None = None,
    answer_query: str | None = None,
) -> dict[str, Any]:
    """Mode D: Subgraph retrieval — entities + relations + blocks as one bundle.

    ``use_reranker`` toggles the cross-encoder step independently of the global
    ``reranker_is_enabled()`` config. The eval harness's ``mode_d_no_rerank``
    variant passes ``False`` to test whether the cross-encoder is the reason
    structural changes get absorbed (see [[reranker_is_ceiling_not_floor]]).

    ``use_topic_keywords`` enables the high-level keyword → relation ANN leg
    that mode_c uses. Off by default to match v1 ablation design; the
    ``mode_d_topic_forced`` variant flips it on so the retrieval pool matches
    mode_c_forced exactly, isolating "subgraph in prompt" as the only variable
    (see [[subgraph-in-prompt-v1-inconclusive]] for the confound this removes).
    """
    t0 = time.time()
    trace: list[dict[str, Any]] = []

    # Step 1 — anchor entities (entity ANN, identical to mode_c step 1)
    ts = time.perf_counter()
    entity_hits = qdrant.search("entities", query_embedding, top_k=top_n_entities)
    e_anchor: dict[str, dict[str, Any]] = {
        h["payload"].get("entity_id", ""): h
        for h in entity_hits
        if h["payload"].get("entity_id")
    }
    top_entities = [
        {"name": h["payload"].get("entity_name", ""), "score": round(h["score"], 3)}
        for h in entity_hits[:5]
        if h["payload"].get("entity_name")
    ]
    trace.append({
        "step": "entity_search",
        "label": "Entity ANN Search",
        "duration_ms": _ms(ts),
        "data": {"hits": len(e_anchor), "top_entities": top_entities},
    })

    # Step 1.5 — TopicKeyword leg (mirrors mode_c step 2). Optional: enabled
    # by ``mode_d_topic_*`` variants so the retrieval pool matches mode_c's
    # entity set and the only remaining variable vs mode_c_forced is the
    # subgraph prompt segment.
    ts = time.perf_counter()
    keywords: list[str] = []
    r_global_entities: set[str] = set()
    if use_topic_keywords:
        keywords = _extract_keywords(query, llm)
        if keywords:
            kw_embs = [embed_query(kw) for kw in keywords]
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
            "disabled": not use_topic_keywords,
        },
    })

    # Step 2 — k-hop expansion (reuse mode_c's batched neighbor fetcher).
    # Seed set is anchor entities ∪ TopicKeyword-derived entities; both are
    # treated as starting nodes for graph traversal.
    ts = time.perf_counter()
    neighbor_weight: dict[str, float] = {}
    e_expanded: set[str] = set(e_anchor.keys()) | r_global_entities
    seed_ids = list(e_expanded)
    neighbor_map = neo4j.get_entity_neighbors_batch(
        seed_ids, max_hops=max_hops, top_m=top_m_neighbors
    )
    for _eid, neighbors in neighbor_map.items():
        for nb in neighbors:
            nbid = nb.get("entity_id", "")
            if not nbid:
                continue
            e_expanded.add(nbid)
            w = float(nb.get("weight") or 0.0)
            if w > neighbor_weight.get(nbid, 0.0):
                neighbor_weight[nbid] = w
    trace.append({
        "step": "graph_traversal",
        "label": "Graph Traversal (RELATES_TO)",
        "duration_ms": _ms(ts),
        "data": {
            "anchor_entities": len(e_anchor),
            "topic_entities": len(r_global_entities),
            "seeds_total": len(seed_ids),
            "expanded_entities": len(e_expanded),
            "hops": max_hops,
        },
    })

    # Step 3 — bound V at ``entity_cap``. Seeds = entity ANN ∪ TopicKeyword
    # entities; both are query-relevant starting points so both are guaranteed
    # quota slots before neighbors fill the tail. When use_topic_keywords=False,
    # r_global_entities is empty and the pruning collapses to the v1 behavior.
    seeds = set(e_anchor.keys()) | r_global_entities
    anchor_quota = min(len(seeds), max(10, entity_cap // 2))
    # Sort anchors by their ANN score (TopicKeyword-only seeds get score 0.0,
    # so they sit at the tail of the anchor list — entity-ANN evidence
    # outranks keyword-derived evidence for the head slots).
    seeds_sorted = sorted(
        seeds,
        key=lambda x: (
            -float(e_anchor[x]["score"]) if x in e_anchor else 0.0,
            x,
        ),
    )[:anchor_quota]
    neighbor_only_sorted = sorted(
        [eid for eid in e_expanded if eid not in seeds],
        key=lambda x: (-neighbor_weight.get(x, 0.0), x),
    )
    remaining = max(0, entity_cap - len(seeds_sorted))
    V: set[str] = set(seeds_sorted) | set(neighbor_only_sorted[:remaining])
    # `anchor_sorted` is kept as an alias for the prompt-rendering step below
    # to avoid touching code further down.
    anchor_sorted = seeds_sorted

    # Step 4 — bound E: internal RELATES_TO edges, anchor-anchor preferred.
    # Anchor set for the edge ordering = full seed set (ANN + TopicKeyword) so
    # cross-seed bridges get surfaced regardless of which leg produced them.
    ts = time.perf_counter()
    edges = neo4j.get_relations_among_entities(
        list(V),
        limit=edge_cap,
        anchor_ids=list(seeds),
    )
    trace.append({
        "step": "edge_fetch",
        "label": "Subgraph Edge Fetch",
        "duration_ms": _ms(ts),
        "data": {
            "v_size": len(V),
            "e_size": len(edges),
            "edge_cap": edge_cap,
        },
    })

    # Step 5 — supporting blocks B + α/β/γ scoring (identical to mode_c)
    ts = time.perf_counter()
    candidate_blocks: dict[str, dict[str, Any]] = {}
    block_entity_coverage: dict[str, set[str]] = {}

    entity_blocks_map = neo4j.get_blocks_for_entities_batch(list(V))
    for eid, blocks in entity_blocks_map.items():
        for b in blocks:
            bid = b.get("block_id", "")
            if not bid:
                continue
            if bid not in candidate_blocks:
                candidate_blocks[bid] = b
                block_entity_coverage[bid] = set()
            block_entity_coverage[bid].add(eid)

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

    candidate_bids = [bid for bid, _, _ in scored]
    webpage_map = neo4j.get_webpages_for_blocks_batch(candidate_bids)

    # First-stage candidate cap mirrors mode_c — bounded pool size regardless
    # of whether we then rerank. Cap at first_stage_top_k unconditionally so
    # mode_d_no_rerank doesn't see a strictly larger pool than the rerank
    # variants — that would confound "skip rerank" with "see more candidates".
    rerank_on = use_reranker and reranker_is_enabled()
    first_stage_cap = reranker_first_stage_top_k()
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

    # Step 6 — optional cross-encoder rerank (the variable mode_d_no_rerank flips)
    if rerank_on and pool_blocks:
        ts_rr = time.perf_counter()
        reranked = rerank_blocks(
            query=query,
            blocks=pool_blocks,
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
    else:
        reranked = pool_blocks
        trace.append({
            "step": "rerank",
            "label": "Cross-Encoder Rerank",
            "duration_ms": 0,
            "data": {"candidates": len(pool_blocks), "kept": len(pool_blocks),
                     "skipped": True, "reason": "use_reranker=False" if not use_reranker
                                                 else "globally_disabled"},
        })

    # Step 7 — fetch entity rows for the prompt, BEFORE budget calc (need their
    # rendered length to subtract from CONTEXT_BUDGET).
    entity_node_map = neo4j.get_entities_batch(list(V))
    entity_rows: list[dict[str, Any]] = []
    # Anchors first (most relevant to the query), then neighbors by path_weight.
    # When use_topic_keywords=True, ``seeds`` includes both entity-ANN and
    # TopicKeyword-derived entities — both render with [anchor] in the prompt
    # to mark them as directly query-relevant (vs hop-derived neighbors).
    anchor_set = seeds
    ordered_v = [eid for eid in anchor_sorted if eid in V] + [
        eid for eid in neighbor_only_sorted if eid in V and eid not in anchor_set
    ]
    for eid in ordered_v:
        node = entity_node_map.get(eid)
        if not node:
            continue
        entity_rows.append({
            "entity_id": eid,
            "entity_name": node.get("entity_name") or eid,
            "entity_type": node.get("entity_type") or "",
            "description": node.get("description") or "",
            "is_anchor": eid in anchor_set,
        })

    # Step 8 — token-budget selection. Subgraph segment is accounted FIRST so
    # the block loop sees the residual budget (plan risk #3).
    subgraph_tokens = _estimate_subgraph_tokens(entity_rows, edges)
    block_budget = max(500, CONTEXT_BUDGET - subgraph_tokens)
    # Cap final block count at final_top_k regardless of rerank state. Without
    # this, mode_d_no_rerank would let the budget loop run until tokens
    # exhausted (~40-60 blocks observed in smoke test), making the paired
    # comparison test "more blocks" rather than "subgraph + skip rerank".
    final_cap = reranker_final_top_k()
    if rerank_on and mmr_is_enabled() and len(reranked) > final_cap:
        mmr_head = mmr_select(reranked, final_k=final_cap)
        seen_ids = {b.get("block_id") for b in mmr_head}
        tail = [b for b in reranked if b.get("block_id") not in seen_ids]
        reranked = mmr_head + tail

    selected: list[dict[str, Any]] = []
    total_tokens = 0
    page_counts: dict[str, int] = {}
    for entry in reranked:
        if total_tokens >= block_budget or len(selected) >= final_cap:
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
            "subgraph_tokens_est": subgraph_tokens,
            "block_budget": block_budget,
        },
    })

    # Step 9 — render subgraph-aware prompt + answer
    ts = time.perf_counter()
    related_links = collect_related_links(selected, neo4j)
    prompt = _ANSWER_TMPL.render(
        query=answer_query or query,
        entities=entity_rows,
        relations=edges,
        entity_desc_chars=SUBGRAPH_DESC_CHARS,
        relation_desc_chars=SUBGRAPH_REL_DESC_CHARS,
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
        "mode": "D",
        "blocks": selected,
        "subgraph": {
            "entities": entity_rows,
            "relations": edges,
            "anchor_entity_ids": list(e_anchor.keys()),
            "topic_entity_ids": list(r_global_entities),
            "v_size": len(V),
            "e_size": len(edges),
        },
        "entities_expanded": len(e_expanded),
        "entities_after_pruning": len(V),
        "edges_kept": len(edges),
        "keywords_extracted": keywords,
        "use_reranker": rerank_on,
        "use_topic_keywords": use_topic_keywords,
        "answer_prompt": prompt,
        "elapsed_seconds": round(elapsed, 2),
        "trace": trace,
    }
