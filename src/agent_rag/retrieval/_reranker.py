"""SiliconFlow BGE-reranker-v2-m3 client with SQLite-backed cache.

Implements second-stage cross-encoder reranking over first-stage ANN
candidates. Used by mode_a / mode_b / mode_c to lift recall@5 from the
first-stage 0.12 floor towards the 0.5–0.6 ceiling that Phase 0
diagnostics revealed (see docs/eval/phaseA_recall50_diag_20q_report.md).

API: SiliconFlow's Cohere-compatible /v1/rerank endpoint.
Cache: SQLite WAL, keyed by sha256(model + query + sorted(block_ids)).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from agent_rag.config import llm_config, settings, thresholds_config

logger = structlog.get_logger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────

import os

_reranker_cfg = llm_config.get("reranker", {}) or {}
# RERANKER_MODEL env var lets eval harness A/B different models without
# editing yaml between runs (e.g. RERANKER_MODEL=Qwen/Qwen3-Reranker-4B).
_RERANKER_MODEL = os.environ.get("RERANKER_MODEL") or _reranker_cfg.get(
    "model", "BAAI/bge-reranker-v2-m3"
)
_RERANKER_PROVIDER = _reranker_cfg.get("provider", "siliconflow")
_RERANKER_TIMEOUT = float(_reranker_cfg.get("timeout_seconds", 30.0))
_RERANKER_MAX_RETRIES = int(_reranker_cfg.get("max_retries", 2))

_retrieval_cfg = thresholds_config.get("retrieval", {}) or {}
_RERANKER_ENABLED = bool(_retrieval_cfg.get("reranker_enabled", True))
_FIRST_STAGE_TOP_K = int(_retrieval_cfg.get("first_stage_top_k", 50))
_FINAL_TOP_K = int(_retrieval_cfg.get("final_top_k", 10))

# RERANKER_MODE env var picks the reranking algorithm without editing yaml.
# Used by the "reranker absorption" diagnostic — checking whether KG-mode
# differentiated candidates regain top-5 placement once the cross-encoder
# stops collapsing them.
#
#   cross_encoder   — default; current behavior (BGE/Qwen3 cross-encoder)
#   first_stage_only — skip the cross-encoder entirely, rank by
#                     first_stage_score (with a defensive descending sort).
#                     Tests "is the Entity/hybrid layer's contribution
#                     suppressed by the reranker, or genuinely absent?"
#   rrf             — Reciprocal Rank Fusion of the cross-encoder rank and
#                     the first_stage_score rank. Lighter-touch alternative
#                     to first_stage_only.
_RERANKER_MODE = (
    os.environ.get("RERANKER_MODE")
    or _retrieval_cfg.get("reranker_mode", "cross_encoder")
).lower()
_VALID_RERANKER_MODES = {"cross_encoder", "first_stage_only", "rrf"}
if _RERANKER_MODE not in _VALID_RERANKER_MODES:
    raise ValueError(
        f"Invalid RERANKER_MODE={_RERANKER_MODE!r}; "
        f"expected one of {sorted(_VALID_RERANKER_MODES)}"
    )

# RRF constant k (Cormack et al. 2009). 60 is the standard literature value.
_RRF_K = int(_retrieval_cfg.get("rrf_k", 60))

# Score-fusion weight: final = α·norm(rerank) + (1-α)·norm(first_stage).
# α=1.0 reproduces the pure-rerank behavior. α=0.7 keeps the cross-encoder
# dominant but stops it from completely burying an ANN top-1 candidate, which
# trusted-25 probes showed happening for ~4/9 remaining misses.
_RERANKER_FUSION_ALPHA = float(
    os.environ.get("RERANKER_FUSION_ALPHA")
    or _retrieval_cfg.get("reranker_fusion_alpha", 1.0)
)

# Cohere-style rerank endpoint truncates documents on the server, but a hard
# client-side cap keeps the request payload predictable and protects the
# cache key from drifting when a block's content grows over time.
_DOC_MAX_CHARS = 1500

# Bumped whenever _block_text composition changes; baked into _cache_key so
# stored scores from the old format are not served against new doc text.
_DOC_FORMAT_VERSION = 2

# ─── Usage accounting (parallels llm/client._USAGE_TOTALS) ────────────────

_USAGE_TOTALS: dict[str, int] = {
    "rerank_calls": 0,
    "rerank_documents": 0,
    "rerank_cache_hits": 0,
    "rerank_cache_misses": 0,
}
_USAGE_LOCK = threading.Lock()


def snapshot_usage() -> dict[str, int]:
    with _USAGE_LOCK:
        return dict(_USAGE_TOTALS)


def diff_usage(prev: dict[str, int]) -> dict[str, int]:
    with _USAGE_LOCK:
        return {k: _USAGE_TOTALS[k] - prev.get(k, 0) for k in _USAGE_TOTALS}


# ─── SQLite cache (WAL + thread-local conn) ──────────────────────────────

_CACHE_DB = Path(__file__).resolve().parents[3] / "data" / "cache" / "rerank_cache.sqlite"
_tls = threading.local()


def _get_cache_conn() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn
    _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rerank_cache ("
        "  key TEXT PRIMARY KEY,"
        "  response TEXT NOT NULL,"
        "  created_at REAL NOT NULL"
        ")"
    )
    conn.commit()
    _tls.conn = conn
    return conn


def _cache_key(model: str, query: str, block_ids: list[str]) -> str:
    # _DOC_FORMAT_VERSION bumps when _block_text changes — otherwise we'd
    # serve stale scores computed against the previous doc format.
    payload = json.dumps(
        {"m": model, "q": query, "ids": sorted(block_ids), "v": _DOC_FORMAT_VERSION},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict[str, float] | None:
    conn = _get_cache_conn()
    row = conn.execute(
        "SELECT response FROM rerank_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _cache_put(key: str, scores: dict[str, float]) -> None:
    conn = _get_cache_conn()
    conn.execute(
        "INSERT OR REPLACE INTO rerank_cache (key, response, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(scores, ensure_ascii=False), time.time()),
    )
    conn.commit()


# ─── HTTP client ──────────────────────────────────────────────────────────

_http_client: httpx.Client | None = None
_HTTP_LOCK = threading.Lock()


def _resolve_credentials() -> tuple[str, str]:
    if _RERANKER_PROVIDER != "siliconflow":
        raise ValueError(
            f"Unsupported reranker provider: {_RERANKER_PROVIDER}. "
            "Only 'siliconflow' is supported."
        )
    api_key = settings.siliconflow_api_key
    base_url = settings.siliconflow_base_url.rstrip("/")
    if not api_key:
        raise ValueError(
            "SILICONFLOW_API_KEY is required for reranker. Set it in .env."
        )
    return api_key, base_url


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is not None:
        return _http_client
    with _HTTP_LOCK:
        if _http_client is None:
            _http_client = httpx.Client(timeout=_RERANKER_TIMEOUT)
    return _http_client


def _truncate_doc(text: str) -> str:
    if len(text) <= _DOC_MAX_CHARS:
        return text
    return text[:_DOC_MAX_CHARS]


def _call_rerank_api(
    query: str,
    documents: list[str],
    top_n: int,
) -> list[tuple[int, float]]:
    """Call SiliconFlow /v1/rerank, return [(index, score), ...] sorted desc.

    Raises on persistent failure; callers must handle for graceful fallback.
    """
    api_key, base_url = _resolve_credentials()
    url = f"{base_url}/rerank"
    payload = {
        "model": _RERANKER_MODEL,
        "query": query,
        "documents": documents,
        "top_n": top_n,
        # Echo documents back. The parser below only uses index/relevance_score,
        # but a LiteLLM proxy in front of SiliconFlow requires the document field
        # to satisfy its RerankResponse validation (it rejects null documents).
        # Harmless on a direct SiliconFlow connection (just a larger response).
        "return_documents": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    client = _get_http_client()
    last_exc: Exception | None = None
    for attempt in range(_RERANKER_MAX_RETRIES + 1):
        try:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            return [
                (int(r["index"]), float(r["relevance_score"]))
                for r in results
                if "index" in r and "relevance_score" in r
            ]
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "rerank_api_error",
                attempt=attempt,
                error=str(exc),
                doc_count=len(documents),
            )
            if attempt < _RERANKER_MAX_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"reranker failed after {_RERANKER_MAX_RETRIES + 1} attempts") from last_exc


# ─── Public API ───────────────────────────────────────────────────────────


def is_enabled() -> bool:
    return _RERANKER_ENABLED


def mode() -> str:
    return _RERANKER_MODE


def first_stage_top_k() -> int:
    return _FIRST_STAGE_TOP_K


def final_top_k() -> int:
    return _FINAL_TOP_K


def _block_text(block: dict[str, Any]) -> str:
    """Build the document text fed to the cross-encoder.

    Prepends source_title so the reranker sees the same page-level anchor
    that the ANN index was built with (build_index.py `stage_write_base`
    embeds `page_title\n\nblock_content`). Without this, a CHC block whose
    content is entirely in Chinese gets scored against an English query
    with no anchor — even though ANN found it correctly via the title.
    """
    title = (block.get("source_title") or "").strip()
    heading = (block.get("heading_context") or "").strip()
    content = (block.get("content") or "").strip()
    parts = [p for p in (title, heading, content) if p]
    text = "\n\n".join(parts)
    return _truncate_doc(text)


def rerank_blocks(
    query: str,
    blocks: list[dict[str, Any]],
    top_n: int | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Rerank candidate blocks with the cross-encoder, return top_n.

    Each returned block keeps its original keys and gains:
      - ``rerank_score`` (float, descending)
      - ``first_stage_score`` (the previous score, if any)
      - ``first_stage_rank`` (1-based position before rerank)

    Behavior:
      - If reranker is disabled, falls back to original order (truncated to top_n).
      - On API failure, falls back to original order with a warning.
      - Blocks lacking text content are skipped from the rerank call but kept
        at the tail of the result with rerank_score=0.0.
    """
    if not blocks:
        return []

    effective_top_n = top_n if top_n is not None else _FINAL_TOP_K
    effective_top_n = min(effective_top_n, len(blocks))

    if not _RERANKER_ENABLED:
        return _annotate_fallback(blocks[:effective_top_n], reason="disabled")

    # ── first_stage_only mode: skip cross-encoder entirely ──────────────
    # Reranker-absorption diagnostic: by ordering on first_stage_score we
    # check whether mode_c / hybrid candidate pools — once decoupled from
    # the cross-encoder — re-surface gold blocks the cross-encoder was
    # collapsing back to mode_a's top-5.
    if _RERANKER_MODE == "first_stage_only":
        sorted_blocks = sorted(
            blocks,
            key=lambda b: (-float(b.get("score") or 0.0), str(b.get("block_id", ""))),
        )
        return _annotate_fallback(
            sorted_blocks[:effective_top_n], reason="first_stage_only"
        )

    documents: list[str] = []
    doc_indices: list[int] = []
    for idx, b in enumerate(blocks):
        text = _block_text(b)
        if text:
            documents.append(text)
            doc_indices.append(idx)

    if not documents:
        return _annotate_fallback(blocks[:effective_top_n], reason="no_text")

    block_ids = [str(blocks[i].get("block_id", f"_idx{i}")) for i in doc_indices]
    cache_key = _cache_key(_RERANKER_MODEL, query, block_ids) if use_cache else ""

    score_map: dict[str, float] | None = None
    cache_hit = False
    if use_cache and cache_key:
        score_map = _cache_get(cache_key)
        if score_map is not None:
            cache_hit = True

    if score_map is None:
        try:
            ranked = _call_rerank_api(
                query=query,
                documents=documents,
                top_n=len(documents),
            )
        except Exception as exc:
            logger.warning("rerank_fallback_to_first_stage", error=str(exc))
            return _annotate_fallback(blocks[:effective_top_n], reason="api_error")

        # Map back from doc-list index (within documents[]) to block_id so the
        # cache survives reordering of the input list across calls.
        score_map = {}
        for doc_idx, score in ranked:
            if 0 <= doc_idx < len(block_ids):
                score_map[block_ids[doc_idx]] = score
        if use_cache and cache_key:
            _cache_put(cache_key, score_map)

    with _USAGE_LOCK:
        _USAGE_TOTALS["rerank_calls"] += 1
        _USAGE_TOTALS["rerank_documents"] += len(documents)
        if cache_hit:
            _USAGE_TOTALS["rerank_cache_hits"] += 1
        else:
            _USAGE_TOTALS["rerank_cache_misses"] += 1

    enriched: list[dict[str, Any]] = []
    for orig_rank, b in enumerate(blocks, start=1):
        bid = str(b.get("block_id", ""))
        first_stage_score = b.get("score")
        merged = dict(b)
        merged["first_stage_score"] = first_stage_score
        merged["first_stage_rank"] = orig_rank
        rerank_score = score_map.get(bid)
        if rerank_score is None:
            merged["rerank_score"] = 0.0
            merged["rerank_missing"] = True
        else:
            merged["rerank_score"] = float(rerank_score)
        merged["score"] = merged["rerank_score"]
        enriched.append(merged)

    # Score fusion: min-max normalize first_stage_score and rerank_score across
    # the candidate pool, then blend with α (config key reranker_fusion_alpha).
    # α=1.0 → identical to pure rerank ordering (the original behavior).
    # α<1.0 → ANN-strong / rerank-weak candidates get partial credit, so an
    # ANN top-1 can't be completely buried.
    if _RERANKER_MODE == "rrf":
        # RRF fusion: rank by cross-encoder, rank by first_stage_score, then
        # sum reciprocals 1/(k + rank). Tied ranks share their position.
        present = [b for b in enriched if not b.get("rerank_missing")]
        if len(present) >= 2:
            # Sort by rerank desc → rerank_rank (1-based)
            present_by_rerank = sorted(
                present, key=lambda b: -float(b.get("rerank_score") or 0.0)
            )
            rerank_rank = {
                id(b): i + 1 for i, b in enumerate(present_by_rerank)
            }
            # Sort by first_stage desc → first_rank (1-based)
            present_by_first = sorted(
                present,
                key=lambda b: -float(b.get("first_stage_score") or 0.0),
            )
            first_rank = {
                id(b): i + 1 for i, b in enumerate(present_by_first)
            }
            for b in present:
                rr = rerank_rank[id(b)]
                fr = first_rank[id(b)]
                rrf = 1.0 / (_RRF_K + rr) + 1.0 / (_RRF_K + fr)
                b["rrf_score"] = rrf
                b["rerank_rank"] = rr
                b["first_stage_rank_fused"] = fr
                b["score"] = rrf
    elif _RERANKER_FUSION_ALPHA < 1.0:
        def _norm(values: list[float]) -> list[float]:
            if not values:
                return values
            lo, hi = min(values), max(values)
            span = hi - lo
            if span <= 1e-9:
                return [0.0 for _ in values]
            return [(v - lo) / span for v in values]

        present = [b for b in enriched if not b.get("rerank_missing")]
        if len(present) >= 2:
            r_norm = _norm([float(b["rerank_score"]) for b in present])
            f_norm = _norm([float(b.get("first_stage_score") or 0.0) for b in present])
            alpha = _RERANKER_FUSION_ALPHA
            for b, rn, fn in zip(present, r_norm, f_norm, strict=False):
                fused = alpha * rn + (1.0 - alpha) * fn
                b["rerank_score_raw"] = b["rerank_score"]
                b["first_stage_score_norm"] = fn
                b["rerank_score_norm"] = rn
                b["fused_score"] = fused
                b["score"] = fused

    enriched.sort(
        key=lambda x: (
            x.get("rerank_missing", False),
            -float(x.get("score", 0.0)),
            x.get("first_stage_rank", 9999),
        )
    )
    return enriched[:effective_top_n]


def _annotate_fallback(
    blocks: list[dict[str, Any]],
    reason: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for orig_rank, b in enumerate(blocks, start=1):
        merged = dict(b)
        merged.setdefault("first_stage_score", b.get("score"))
        merged.setdefault("first_stage_rank", orig_rank)
        merged["rerank_score"] = None
        merged["rerank_fallback"] = reason
        out.append(merged)
    return out


__all__ = [
    "rerank_blocks",
    "is_enabled",
    "first_stage_top_k",
    "final_top_k",
    "snapshot_usage",
    "diff_usage",
]
