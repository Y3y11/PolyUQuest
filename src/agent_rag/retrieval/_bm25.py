"""BM25 sparse retrieval for block-level search (hybrid first-stage).

The dense ANN retriever (BGE-M3 + Qdrant) misses some questions where the gold
block contains the literal terms from the query but lives in a semantically
crowded neighbourhood. BM25 is a cheap sparse complement: it lights up exact
term overlap, which is precisely what dense misses on PolyU department
acronyms (CEE / LST / ENGL), proper-noun event names ("Short-Essay
Competition", "Inaugural"), and date strings.

This module owns a lazy-loaded in-memory BM25 index over every block's
(heading_context + content) text, sourced from Qdrant payloads on first use. We
do not persist the index to disk — building 47k docs takes ~1s, so loading the
index from Qdrant on retrieval-process startup is fast enough.

Surface:
    search(query, top_k) -> list[{"payload": {...}, "score": float}]
    The payload shape mirrors Qdrant hits so callers can union the two pools
    without bespoke adapter code.
"""

from __future__ import annotations

import pickle
import re
import threading
import time
from pathlib import Path
from typing import Any

import structlog
from rank_bm25 import BM25Okapi

from agent_rag.storage.qdrant_store import QdrantStore

logger = structlog.get_logger(__name__)

# On-disk cache: building from Qdrant takes ~8 min on a 42k-block corpus
# (Qdrant scroll dominates, BM25Okapi construction is non-trivial too), so
# the first query after a server restart would otherwise wait for the build.
# We persist (block_ids, payloads, BM25Okapi) and reload if the qdrant block
# count is unchanged. If the corpus is re-ingested, count differs → rebuild.
_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "cache" / "bm25_index.pkl"
)
_CACHE_VERSION = 1

# Token regex: alnum runs only. PolyU corpus is dominantly English with some
# CJK in titles — we keep the simple path and let BGE-M3 handle CJK on the
# dense side. CJK in queries against CJK-heavy blocks is rare enough that
# stretching BM25 there would not justify the multilingual tokenizer cost.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


_INDEX: BM25Okapi | None = None
_BLOCK_IDS: list[str] = []
_PAYLOADS: list[dict[str, Any]] = []
_LOCK = threading.Lock()


def _qdrant_block_count(qd: QdrantStore) -> int:
    try:
        return int(qd._client.count(collection_name="blocks", exact=True).count)  # noqa: SLF001
    except Exception as exc:
        logger.warning("bm25_count_failed", error=str(exc))
        return -1


def _try_load_cache(expected_count: int) -> bool:
    """Return True if the on-disk cache matched the live qdrant block count and
    populated the module globals."""
    global _INDEX, _BLOCK_IDS, _PAYLOADS
    if not _CACHE_PATH.exists():
        return False
    try:
        t0 = time.time()
        with _CACHE_PATH.open("rb") as f:
            blob = pickle.load(f)
    except Exception as exc:
        logger.warning("bm25_cache_load_failed", error=str(exc))
        return False
    if not isinstance(blob, dict) or blob.get("version") != _CACHE_VERSION:
        return False
    cached_count = int(blob.get("n_blocks", -1))
    if expected_count >= 0 and cached_count != expected_count:
        logger.info(
            "bm25_cache_stale",
            cached=cached_count,
            qdrant=expected_count,
        )
        return False
    _INDEX = blob["index"]
    _BLOCK_IDS = blob["block_ids"]
    _PAYLOADS = blob["payloads"]
    logger.info(
        "bm25_cache_loaded",
        n_blocks=len(_BLOCK_IDS),
        elapsed_s=round(time.time() - t0, 2),
    )
    return True


def _save_cache() -> None:
    if _INDEX is None or not _BLOCK_IDS:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".pkl.tmp")
        with tmp.open("wb") as f:
            pickle.dump(
                {
                    "version": _CACHE_VERSION,
                    "n_blocks": len(_BLOCK_IDS),
                    "index": _INDEX,
                    "block_ids": _BLOCK_IDS,
                    "payloads": _PAYLOADS,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        tmp.replace(_CACHE_PATH)
        logger.info("bm25_cache_saved", path=str(_CACHE_PATH))
    except Exception as exc:
        logger.warning("bm25_cache_save_failed", error=str(exc))


def _build_index() -> None:
    """Populate the BM25 index — from disk cache if available, otherwise scroll
    every block payload from Qdrant and rebuild.

    Called once per process under the module lock; subsequent search calls
    reuse the cached index. Cold rebuild cost on a 42k-block corpus is ~8 min,
    so we cache the result to ``data/cache/bm25_index.pkl`` keyed on the
    qdrant block count.
    """
    global _INDEX, _BLOCK_IDS, _PAYLOADS
    if _INDEX is not None:
        return

    qd = QdrantStore()
    block_ids: list[str] = []
    payloads: list[dict[str, Any]] = []
    docs: list[list[str]] = []
    try:
        expected_count = _qdrant_block_count(qd)
        if _try_load_cache(expected_count):
            return

        t0 = time.time()
        offset = None
        while True:
            pts, offset = qd._client.scroll(  # noqa: SLF001
                collection_name="blocks",
                scroll_filter=None,
                limit=2000,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )
            if not pts:
                break
            for p in pts:
                pl = p.payload or {}
                bid = pl.get("block_id")
                if not bid:
                    continue
                heading = (pl.get("heading_context") or "").strip()
                content = (pl.get("content") or "").strip()
                doc_text = f"{heading}\n{content}" if heading else content
                block_ids.append(bid)
                payloads.append(pl)
                docs.append(_tokenize(doc_text))
            if offset is None:
                break
    finally:
        qd.close()

    if not docs:
        logger.warning("bm25_index_empty_corpus")
        _INDEX = BM25Okapi([["__placeholder__"]])  # avoid divide-by-zero on empty
        _BLOCK_IDS = []
        _PAYLOADS = []
        return

    _INDEX = BM25Okapi(docs)
    _BLOCK_IDS = block_ids
    _PAYLOADS = payloads
    logger.info(
        "bm25_index_built",
        n_blocks=len(block_ids),
        elapsed_s=round(time.time() - t0, 2),
    )
    _save_cache()


def warmup() -> None:
    """Build (or load) the BM25 index now. Safe to call multiple times. Used
    by the API startup hook so the first user query doesn't wait."""
    if _INDEX is not None:
        return
    with _LOCK:
        _build_index()


def search(query: str, top_k: int) -> list[dict[str, Any]]:
    """Return BM25 top-k hits in the same {"payload", "score"} shape as Qdrant.

    Negative or zero BM25 scores are kept — caller decides whether to filter.
    """
    if not query.strip() or top_k <= 0:
        return []
    if _INDEX is None:
        with _LOCK:
            _build_index()
    assert _INDEX is not None
    if not _BLOCK_IDS:
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    scores = _INDEX.get_scores(tokens)
    # argsort descending by score; rank-bm25 returns numpy array
    n = len(_BLOCK_IDS)
    order = sorted(range(n), key=lambda i: -float(scores[i]))[:top_k]
    return [
        {
            "payload": _PAYLOADS[idx],
            "score": float(scores[idx]),
        }
        for idx in order
    ]


def reset_index() -> None:
    """Drop the cached index — next search will rebuild from Qdrant. For tests
    and for after-build refresh scripts."""
    global _INDEX, _BLOCK_IDS, _PAYLOADS
    with _LOCK:
        _INDEX = None
        _BLOCK_IDS = []
        _PAYLOADS = []


def expand_candidates(
    query: str,
    existing_blocks: list[dict[str, Any]],
    top_k: int,
) -> tuple[list[dict[str, Any]], int]:
    """Union BM25-hit blocks into ``existing_blocks``, skipping duplicates.

    Returns (expanded_list, num_new_added). Each new entry uses the same shape
    that callers already pass to the cross-encoder reranker: block_id,
    content, heading_context, source_url, source_title, score. BM25 score is
    stored under ``bm25_score`` so it stays out of cosine-comparable
    aggregations downstream; ``score`` is set to 0.0 because BM25 scores are
    not on the [0,1] cosine scale.
    """
    if top_k <= 0 or not query.strip():
        return existing_blocks, 0

    seen = {b.get("block_id") for b in existing_blocks if b.get("block_id")}
    added = 0
    out = list(existing_blocks)
    for hit in search(query, top_k=top_k):
        pl = hit["payload"]
        bid = pl.get("block_id")
        if not bid or bid in seen:
            continue
        out.append({
            "block_id": bid,
            "content": pl.get("content", ""),
            "heading_context": pl.get("heading_context", ""),
            "source_url": pl.get("url", ""),
            "source_title": "",  # not in qdrant block payload; reranker will fetch
            "score": 0.0,
            "bm25_score": float(hit["score"]),
            "from_bm25": True,
        })
        seen.add(bid)
        added += 1
    return out, added
