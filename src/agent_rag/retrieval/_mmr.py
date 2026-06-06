"""MMR (Maximal Marginal Relevance) diversification, post-rerank.

Trade off relevance vs redundancy when picking the final top-K from a larger
reranked pool. Targets two failure modes identified on V5 entity_list (β-3):

  1. Cross-domain mirror displacement (q_entity_list_013): the same content is
     indexed N times under different polyu.edu.hk hosts/paths, all reranked at
     rr≈0.999. They occupy top-1..top-N and push the second gold (different
     content, same intent) out of top-5.

  2. Hub-page near-duplicates (q_entity_list_003): JS3006 jupas / non-jupas /
     international variants are reranked at rr≈0.996 each. Same problem.

MMR selects the next block to add by maximizing:

    score(b) = λ · rerank_norm(b) − (1−λ) · max(sim(b, b') for b' in selected)

where similarity = max(url_match, content_jaccard). λ=1.0 reproduces pure
rerank order. λ=0.5 is balanced. λ→0 maximizes diversity at the cost of
relevance.

Gated by env `MMR_LAMBDA` (float in [0,1], default 0.0 = disabled / passthrough).
"""

from __future__ import annotations

import os
import re
from typing import Any

_MMR_LAMBDA = float(os.environ.get("MMR_LAMBDA", "0.0") or "0.0")
_MMR_URL_WEIGHT = float(os.environ.get("MMR_URL_WEIGHT", "1.0") or "1.0")

_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]+")


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _block_signature(b: dict[str, Any]) -> tuple[str, set[str]]:
    url = (b.get("source_url") or "").strip()
    text = (b.get("content") or "")[:1500]
    return url, _tokens(text)


def _similarity(a_url: str, a_toks: set[str], b_url: str, b_toks: set[str]) -> float:
    url_sim = _MMR_URL_WEIGHT if (a_url and a_url == b_url) else 0.0
    return max(url_sim, _jaccard(a_toks, b_toks))


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return values
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 1e-9:
        return [1.0 for _ in values]
    return [(v - lo) / span for v in values]


def mmr_is_enabled() -> bool:
    return _MMR_LAMBDA > 0.0


def mmr_lambda() -> float:
    return _MMR_LAMBDA


def mmr_select(
    blocks: list[dict[str, Any]],
    final_k: int,
    *,
    lambda_: float | None = None,
) -> list[dict[str, Any]]:
    """Greedy MMR over a rerank pool. Expects blocks already sorted by rerank_score
    desc (the rerank_blocks output order). Returns up to final_k blocks in the
    selection order MMR produced (NOT rerank order).

    If MMR is disabled (lambda<=0) or the pool is small, returns blocks[:final_k]
    unchanged.
    """
    if not blocks:
        return []
    lam = _MMR_LAMBDA if lambda_ is None else lambda_
    if lam <= 0.0 or final_k <= 0:
        return blocks[:final_k]
    if len(blocks) <= final_k:
        return list(blocks)

    rerank_scores = [
        float(b.get("rerank_score") or b.get("score") or 0.0) for b in blocks
    ]
    rerank_norm = _normalize(rerank_scores)
    sigs = [_block_signature(b) for b in blocks]

    n = len(blocks)
    remaining = set(range(n))
    selected: list[int] = []
    # First pick: highest relevance (MMR with empty selection collapses to pure rerank)
    first = max(remaining, key=lambda i: rerank_norm[i])
    selected.append(first)
    remaining.remove(first)

    max_sim_to_selected = [0.0] * n
    for i in remaining:
        max_sim_to_selected[i] = _similarity(*sigs[i], *sigs[first])

    while remaining and len(selected) < final_k:
        best_i = -1
        best_score = -1e9
        for i in remaining:
            mmr = lam * rerank_norm[i] - (1.0 - lam) * max_sim_to_selected[i]
            if mmr > best_score:
                best_score = mmr
                best_i = i
        selected.append(best_i)
        remaining.remove(best_i)
        for j in remaining:
            sim = _similarity(*sigs[j], *sigs[best_i])
            if sim > max_sim_to_selected[j]:
                max_sim_to_selected[j] = sim

    return [blocks[i] for i in selected]
