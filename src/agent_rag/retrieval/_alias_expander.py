"""Acronym-aware query expansion for PolyU-specific terminology.

Problem: BGE-M3 has weak alignment between PolyU department acronyms (CHC, LST,
CEE, ISE, ...) and their full canonical names. Trusted-25 diagnostic showed
4/8 miss-top-100 questions are gated by exactly this acronym blind spot.

Fix: before embedding a *query*, expand recognised acronyms in place by
appending the canonical name. The block embeddings already contain the full
names (titles say "Department of Computing"), so the expanded query lands
much closer in BGE-M3 space.

Scope: query-side only. Offline document embeddings are untouched — that
preserves the dictionary as a retrieval-time concern.

Rules (keep simple, observable):
  - Variants are loaded from configs/aliases.yaml's ``departments``,
    ``offices``, and ``programmes`` sections.
  - Short variants (<= 3 chars, e.g. "ME", "FH", "AR") match only in ALL CAPS
    with word boundaries — avoids collisions with English pronouns / particles.
  - Longer variants (>= 4 chars, e.g. "COMP", "SHTM") match case-insensitively.
  - If the canonical name (case-insensitive substring) is already in the query,
    no expansion happens — idempotent.
  - Multiple distinct acronyms in one query each contribute their canonical.

Example:
  >>> expand_query("What types of postgraduate degrees in CHC?")
  'What types of postgraduate degrees in CHC? (CHC = Department of Chinese History and Culture)'
"""
from __future__ import annotations

import re
from functools import lru_cache

import structlog

from agent_rag.config import aliases_config

logger = structlog.get_logger(__name__)

_VARIANT_SECTIONS = ("departments", "offices", "programmes")


def _build_variant_index() -> list[tuple[re.Pattern[str], str, str]]:
    """Return [(compiled_pattern, variant, canonical), ...].

    Compiled once at module load. We keep the variant string alongside so the
    log line is informative.
    """
    index: list[tuple[re.Pattern[str], str, str]] = []
    for section in _VARIANT_SECTIONS:
        section_map = aliases_config.get(section, {}) or {}
        for canonical, variants in section_map.items():
            for v in variants or []:
                v_str = str(v).strip()
                if not v_str:
                    continue
                if len(v_str) <= 3:
                    # short acronym: case-sensitive ALL CAPS only
                    pat = re.compile(rf"\b{re.escape(v_str)}\b")
                else:
                    pat = re.compile(rf"\b{re.escape(v_str)}\b", re.IGNORECASE)
                index.append((pat, v_str, canonical))
    return index


@lru_cache(maxsize=1)
def _get_index() -> list[tuple[re.Pattern[str], str, str]]:
    idx = _build_variant_index()
    logger.info("alias_expander_initialized", variants=len(idx))
    return idx


def expand_query(query: str) -> str:
    """Append canonical names for any recognised acronyms in *query*.

    The original query is preserved verbatim; canonicals are appended after it
    inside a parenthetical, e.g.
        "Who teaches in CHC?"
        → "Who teaches in CHC? (CHC = Department of Chinese History and Culture)"

    Returns the original query unchanged when nothing matched.
    """
    if not query:
        return query
    index = _get_index()
    found: list[tuple[str, str]] = []  # preserves insertion order, deduped below
    seen_canonicals: set[str] = set()
    q_lower = query.lower()
    for pat, variant, canonical in index:
        if canonical.lower() in q_lower:
            # already mentions canonical — no need to inject
            seen_canonicals.add(canonical)
            continue
        if pat.search(query) and canonical not in seen_canonicals:
            found.append((variant, canonical))
            seen_canonicals.add(canonical)
    if not found:
        return query
    suffix = "; ".join(f"{v} = {c}" for v, c in found)
    return f"{query} ({suffix})"
