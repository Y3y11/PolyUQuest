"""LLM-driven multilingual query rewriter for first-stage retrieval expansion.

Why this exists:
    The PolyU corpus is multilingual (en + 繁體中文 + occasional 簡體). Many gold
    passages live behind language or paraphrase gaps that the literal English
    query misses — e.g. "language competitions" vs "Korean Speech Contest",
    "new faculty joined 1 April 2026" vs "New joiner – Dr X". Dense ANN + BM25
    on the original query alone cannot bridge these gaps.

    The deterministic alias expander (_alias_expander.py) handles only the
    acronym → canonical case. This module handles the harder cases:
    translation, lexical paraphrase, and proper-noun anchoring.

Design:
    - One small LLM call per *unique* query, cached via the chat cache (the
      same SQLite-backed cache used by routing). Trusted-25 eval re-runs hit
      the cache on every question after the first run.
    - Returns up to 3 rewrites + the original. Callers fan out per rewrite,
      union-dedup the candidate pools, then let the cross-encoder reranker
      decide the final ranking. We do NOT mix rewrite scores in any
      arithmetic — the reranker is the only judge of relevance.
    - If the LLM returns garbage or the JSON parse fails, we degrade
      gracefully to the original query (zero rewrites). The whole pipeline
      keeps working; we just lose this one signal for that query.

Surface:
    rewrite_query(query, llm) -> list[str]
        Returns a list of *additional* query strings (does NOT include the
        original). Callers should explicitly include the original themselves.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import llm_config, thresholds_config
from agent_rag.llm.client import LLMClient, cost_stage

logger = structlog.get_logger(__name__)

_REWRITE_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "rewrite_query.j2")
    .read_text(encoding="utf-8")
)

_cfg = thresholds_config.get("retrieval", {}).get("query_rewrite", {}) or {}
_ENABLED = bool(_cfg.get("enabled", False))
_MAX_REWRITES = int(_cfg.get("max_rewrites", 3))
# Gating threshold — when the router reports confidence >= this value we skip
# the rewriter LLM call. 0.0 disables gating (always rewrite, legacy
# behavior); 1.0 effectively disables the rewriter entirely. The orchestrator
# (evaluate.py / query_router.py) is responsible for calling
# ``set_router_confidence(conf)`` before the retrieval modes run.
_DEFAULT_GATING_THRESHOLD = float(_cfg.get("confidence_threshold", 0.0) or 0.0)

# (confidence, threshold_override). The override lets the ablation harness
# turn gating off per-variant without touching config or env. None = unset.
_gating_ctx: ContextVar[tuple[float | None, float | None]] = ContextVar(
    "_rewriter_gating_ctx", default=(None, None)
)


def set_router_confidence(
    confidence: float | None, threshold_override: float | None = None
) -> Token:
    """Push router confidence (and optional threshold override) onto the
    gating context. Returns a token the caller must pass to
    ``reset_router_confidence`` so nested calls restore the prior frame.
    """
    return _gating_ctx.set((confidence, threshold_override))


def reset_router_confidence(token: Token) -> None:
    _gating_ctx.reset(token)


def _should_skip_for_confidence() -> bool:
    conf, override = _gating_ctx.get()
    threshold = override if override is not None else _DEFAULT_GATING_THRESHOLD
    if threshold <= 0.0 or conf is None:
        return False
    return conf >= threshold
# Dedicated `rewrite` stage in llm.yaml — separate from `generation` so we can
# point it at a lightweight model (Ling-flash-2.0 ~1.3s/call) without dragging
# the answer-generation tier down. Falls back to `generation` then provider
# default if the section is missing.
_REWRITE_CFG = (llm_config.get("rewrite") or {})
_REWRITE_MODEL = (
    _REWRITE_CFG.get("model")
    or (llm_config.get("generation", {}) or {}).get("model")
)
_REWRITE_TEMPERATURE = float(_REWRITE_CFG.get("temperature", 0.0))
_REWRITE_MAX_TOKENS = int(_REWRITE_CFG.get("max_tokens", 256))


def is_enabled() -> bool:
    return _ENABLED


# In-process memoization on top of the LLM cache. The LLM cache already handles
# cross-process persistence; this @lru_cache avoids the SQLite hit for hot
# queries within a single run (e.g. the evaluation harness that calls the
# pipeline multiple times per question across variants).
_cache_lock = Lock()


@lru_cache(maxsize=512)
def _cached_rewrite(query: str) -> tuple[str, ...]:
    """Cached LLM call. Returns a tuple (immutable for lru_cache)."""
    if not query or not query.strip():
        return ()

    prompt = _REWRITE_TMPL.render(query=query)
    llm = LLMClient()
    try:
        chat_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": _REWRITE_TEMPERATURE,
            "max_tokens": _REWRITE_MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "use_cache": True,
        }
        if _REWRITE_MODEL:
            chat_kwargs["model"] = _REWRITE_MODEL
        with cost_stage("rewriter"):
            raw = llm.chat(**chat_kwargs)
    except Exception as exc:
        logger.warning("query_rewrite_llm_error", error=str(exc))
        return ()
    finally:
        try:
            llm.close()
        except Exception:
            pass

    try:
        data = json_repair.loads(raw)
        if not isinstance(data, dict):
            return ()
        rewrites = data.get("rewrites") or []
        if not isinstance(rewrites, list):
            return ()
        # Sanitize: strip, drop empties / duplicates / accidental copies of the
        # original. Keep insertion order.
        seen: set[str] = {query.strip().lower()}
        clean: list[str] = []
        for r in rewrites[:_MAX_REWRITES]:
            if not isinstance(r, str):
                continue
            rs = r.strip()
            key = rs.lower()
            if not rs or key in seen:
                continue
            seen.add(key)
            clean.append(rs)
        return tuple(clean)
    except Exception as exc:
        logger.warning("query_rewrite_parse_error", error=str(exc), raw=raw[:200])
        return ()


def rewrite_query(query: str, llm: LLMClient | None = None) -> list[str]:
    """Generate up to ``max_rewrites`` alternative phrasings for *query*.

    The ``llm`` argument is accepted for API symmetry with other retrieval
    helpers but is ignored — the module builds its own client so that the
    @lru_cache works on plain strings (LLMClient is not hashable).

    Returns an empty list when the feature is disabled, when the query is
    empty, or when the LLM/parse fails. Never raises.
    """
    if not _ENABLED:
        return []
    if _should_skip_for_confidence():
        return []
    with _cache_lock:
        return list(_cached_rewrite(query))
