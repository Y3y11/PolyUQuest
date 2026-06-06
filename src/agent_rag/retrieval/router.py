"""Agent query router: classify queries into Mode A/B/C using LLM + heuristics.

Returns a decision dict with the following fields:
    - mode:         one of "mode_a" | "mode_b" | "mode_c"
    - confidence:   float in [0, 1]; 1.0 for heuristic hits, LLM-reported (or
                    fallback) value for LLM hits
    - alt_mode:     second-best candidate or None
    - rule_matched: the regex string that triggered a heuristic hit, or None
    - source:       "heuristic" | "llm" | "cache" | "default"
    - reasoning:    short human-readable explanation

Callers can inspect ``confidence`` and ``alt_mode`` to decide whether to fire a
hybrid retrieval that runs two modes in parallel and merges results.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import thresholds_config
from agent_rag.llm.client import LLMClient, cost_stage
from agent_rag.retrieval._cache import normalize_query, router_cache

logger = structlog.get_logger(__name__)

_ROUTE_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "route_query.j2")
    .read_text(encoding="utf-8")
)

_router_cfg = thresholds_config.get("retrieval", {}).get("router", {})
_DEFAULT_LLM_CONFIDENCE = float(_router_cfg.get("default_llm_confidence", 0.7))
_DISABLE_HYBRID = bool(_router_cfg.get("disable_hybrid", False))


def _strip_hybrid_if_disabled(decision: dict[str, Any]) -> dict[str, Any]:
    """Null out alt_mode so downstream `alt_mode and conf < th` gates short-circuit.

    See `retrieval.router.disable_hybrid` in configs/thresholds.yaml for the
    diagnostic that motivated this. Confidence is kept as-is for telemetry —
    only the alt_mode field is suppressed.
    """
    if _DISABLE_HYBRID:
        decision["alt_mode"] = None
    return decision

_MODE_B_SIGNALS = [
    r"分别", r"各自", r"respectively", r"和.*是什么",
    r"admission.*fee", r"tuition.*scholarship",
    r"requirements?.*and.*(fee|tuition|scholarship|deadline|cost)",
]

_MODE_C_SIGNALS = [
    r"哪些",
    r"有谁",
    r"有什么老师",
    r"有哪些老师",
    r"哪位老师",
    r"哪些老师",
    r"哪些教授",
    r"which.*professors?",
    r"who.*research",
    r"做.*研究",
    r"研究方向",
]

# v3 diagnostic (2026-05-14): 9/15 mixed_ab queries and 7/15 multi_page queries
# routed to a single mode with confidence ≥0.8 → hybrid never fired and gold on
# the alternate page was missed. Two recurring surface patterns trigger this:
#
#  (1) Comparison / contrast questions ("differ between X and Y", "how does X
#      compare to Y", "differences in ... between ... and ...") need a mode_b
#      backbone (each side typically lives on its own page) PLUS a mode_a
#      backup for the single-page detail on either side. Returning these as a
#      forced hybrid with conf=0.5 makes the router gate fire.
#
#  (2) "Two researchers share / co-authored / collaborate on ..." style
#      questions look like entity-relation queries to the LLM (which assigns
#      mode_c) but the gold actually lives on the individual researcher
#      profile pages — mode_b. Force mode_b + alt=mode_c so the entity-graph
#      still has a vote.
_MODE_B_COMPARE_SIGNALS = [
    r"\bdifferences?\s+(in|between|of)\b",
    r"\bhow\s+do(es)?\s+.+\s+(compare|differ|relate)\b",
    r"\bcompare(d|s)?\s+to\b",
    r"\bcontrast(s|ed)?\s+(with|to)\b",
    r"\bdiffer\s+between\b",
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bhow\s+does\s+.+\s+align\s+with\b",
]

_MODE_B_SHARED_RESEARCHER_SIGNALS = [
    r"\b(two|both|which)\s+researchers?\s+.*\b(share|shared|same|common|co-?author(ed)?|collaborate(d)?)\b",
    r"\bco-?author(ed)?\s+(a\s+paper|paper)\b",
    r"\bjoint\s+publication\b",
    r"\bcommon\s+research\s+(topic|interest|area)\b",
    r"\bshare(d)?\s+(a\s+)?research\s+(interest|topic|area)\b",
]

_VALID_MODES = {"mode_a", "mode_b", "mode_c"}

# Signals that suggest a specific named entity is being asked about (mode_a component)
_MODE_A_PERSON_SIGNALS = [
    r"prof\.?\s+\w+",        # "Prof. Maggie LI", "Prof Smith"
    r"dr\.?\s+\w+",          # "Dr. Chan"
    r"professor\s+\w+",
]


def _has_mode_a_person(query: str) -> bool:
    q_lower = query.lower()
    return any(re.search(p, q_lower) for p in _MODE_A_PERSON_SIGNALS)


def _hybrid_heuristic_route(
    query: str, allowed_modes: frozenset[str] = frozenset(_VALID_MODES)
) -> dict[str, Any] | None:
    """Return a forced low-confidence hybrid decision for known mixed patterns.

    Differs from ``_heuristic_route`` (single-mode, confidence 1.0): these
    patterns are *deliberately* under-confident so the router gate triggers
    the hybrid path. Returns None if no hybrid pattern matched.

    When ``allowed_modes`` excludes a side of the hybrid, the alt_mode is
    rewritten to the next-best allowed mode; if the *primary* side is
    excluded, the hybrid is skipped entirely so the LLM path can pick a clean
    single mode.
    """
    q_lower = query.lower()

    compare_hit = next(
        (p for p in _MODE_B_COMPARE_SIGNALS if re.search(p, q_lower)), None
    )
    shared_hit = next(
        (p for p in _MODE_B_SHARED_RESEARCHER_SIGNALS if re.search(p, q_lower)),
        None,
    )
    if compare_hit and "mode_b" in allowed_modes:
        return {
            "mode": "mode_b",
            "confidence": 0.55,
            "alt_mode": "mode_a" if "mode_a" in allowed_modes else None,
            "rule_matched": compare_hit,
            "source": "heuristic",
            "reasoning": f"comparison/contrast pattern: {compare_hit}",
        }
    if shared_hit and "mode_b" in allowed_modes:
        if "mode_c" in allowed_modes:
            alt = "mode_c"
        elif "mode_a" in allowed_modes:
            alt = "mode_a"
        else:
            alt = None
        return {
            "mode": "mode_b",
            "confidence": 0.55,
            "alt_mode": alt,
            "rule_matched": shared_hit,
            "source": "heuristic",
            "reasoning": f"shared-researcher pattern: {shared_hit}",
        }
    return None


def _heuristic_route(
    query: str, allowed_modes: frozenset[str] = frozenset(_VALID_MODES)
) -> tuple[str | None, str | None]:
    """Return (mode, matched_pattern) or (None, None).

    Returns (None, None) for mixed-intent queries so LLM handles them. When
    ``allowed_modes`` excludes a candidate, that branch is suppressed — used by
    the no_entity_layer ablation where mode_c is physically removed from the
    system and the router must not be aware of it.
    """
    q_lower = query.lower()

    c_hit = next((p for p in _MODE_C_SIGNALS if re.search(p, q_lower)), None)
    b_hit = next((p for p in _MODE_B_SIGNALS if re.search(p, q_lower)), None)

    # Mixed: entity-list signal + specific named person → defer to LLM
    if c_hit and _has_mode_a_person(query):
        return None, None

    if c_hit and "mode_c" in allowed_modes:
        return "mode_c", c_hit
    if b_hit and "mode_b" in allowed_modes:
        return "mode_b", b_hit
    return None, None


def _default_decision(source: str, reasoning: str) -> dict[str, Any]:
    return {
        "mode": "mode_a",
        "confidence": 0.5,
        "alt_mode": None,
        "rule_matched": None,
        "source": source,
        "reasoning": reasoning,
    }


def _coerce_to_allowed(
    mode: str, allowed_modes: frozenset[str], reason_query: str
) -> str:
    """Map a disallowed mode to its best allowed substitute.

    Used by the no_entity_layer ablation: when ``mode_c`` is not in
    ``allowed_modes`` but the LLM (or a cached decision) still returns it, we
    must rewrite it. The mapping mirrors what a system *designed without an
    Entity layer* would route to:

    * Cross-page / contrast / co-occurrence cues → mode_b (multi-page aggregation)
    * Otherwise → mode_a (single-page lookup is the safer default)

    Pure mode_a/mode_b decisions are returned unchanged when allowed.
    """
    if mode in allowed_modes:
        return mode
    if mode == "mode_c" and "mode_c" not in allowed_modes:
        q_lower = reason_query.lower()
        cross_page_cues = (
            "differ", "difference", "compare", "comparison", " vs ", " vs. ",
            "versus", "between", "respectively", "分别", "各自",
            "which professors", "which researchers", "who are the",
            "list all", "all the", "research areas",
        )
        if any(c in q_lower for c in cross_page_cues) and "mode_b" in allowed_modes:
            return "mode_b"
        return "mode_a" if "mode_a" in allowed_modes else next(iter(allowed_modes))
    # Any other disallowed mode (defensive): fall back to mode_a if allowed
    return "mode_a" if "mode_a" in allowed_modes else next(iter(allowed_modes))


def route_query(
    query: str,
    llm: LLMClient | None = None,
    *,
    allowed_modes: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Determine the retrieval mode for a query.

    Heuristic regex is tried first; on hit returns with confidence 1.0.
    Otherwise falls back to LLM classification (cached), and finally to a
    plain default if LLM is unavailable or errors.

    ``allowed_modes`` constrains the output to a subset of {mode_a, mode_b,
    mode_c}. Used by the no_entity_layer ablation to physically remove
    mode_c from the system's awareness. When None, defaults to all three.
    """
    allowed = (
        frozenset(allowed_modes) if allowed_modes is not None else frozenset(_VALID_MODES)
    )
    if not allowed:
        raise ValueError("allowed_modes cannot be empty")

    mode, pattern = _heuristic_route(query, allowed)
    if mode is not None:
        return _strip_hybrid_if_disabled({
            "mode": mode,
            "confidence": 1.0,
            "alt_mode": None,
            "rule_matched": pattern,
            "source": "heuristic",
            "reasoning": f"heuristic match: {pattern}",
        })

    # Hybrid heuristics fire *after* the single-mode heuristics so a clean
    # single-mode query doesn't get pulled into a hybrid by an incidental
    # phrase. They run *before* the LLM because the LLM was observed (v3
    # diagnostic, 2026-05-14) confidently assigning a single mode to these
    # mixed queries and skipping the hybrid path entirely.
    hybrid_decision = _hybrid_heuristic_route(query, allowed)
    if hybrid_decision is not None:
        return _strip_hybrid_if_disabled(hybrid_decision)

    if llm is None:
        return _strip_hybrid_if_disabled(
            _default_decision("default", "no LLM available; defaulting to mode_a")
        )

    # Cache key includes allowed-mode signature so ablation runs don't collide
    # with full-system cached decisions.
    allowed_sig = "+".join(sorted(allowed)) if allowed != frozenset(_VALID_MODES) else ""
    cache_key = normalize_query(query) + (f"|allowed={allowed_sig}" if allowed_sig else "")
    cached = router_cache.get(cache_key)
    if cached is not None:
        cached["source"] = "cache"
        return _strip_hybrid_if_disabled(cached)

    prompt = _ROUTE_TMPL.render(query=query)
    try:
        with cost_stage("router"):
            raw = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
                use_cache=True,
            )
        data = json_repair.loads(raw) or {}
        raw_mode = data.get("mode", "mode_a")
        if raw_mode not in _VALID_MODES:
            raw_mode = "mode_a"
        # Constrain to allowed modes — for no_entity_layer this collapses
        # mode_c to mode_a/mode_b based on query surface cues.
        raw_mode = _coerce_to_allowed(raw_mode, allowed, query)

        raw_conf = data.get("confidence")
        try:
            confidence = float(raw_conf) if raw_conf is not None else _DEFAULT_LLM_CONFIDENCE
        except (TypeError, ValueError):
            confidence = _DEFAULT_LLM_CONFIDENCE
        confidence = max(0.0, min(1.0, confidence))

        alt_mode = data.get("alt_mode")
        if alt_mode not in _VALID_MODES or alt_mode == raw_mode:
            alt_mode = None
        elif alt_mode not in allowed:
            alt_mode = None

        # Auto-fill alt_mode for borderline LLM confidence (0.6–0.75 range)
        # so the hybrid path can still fire. Picks the most-complementary
        # alternate based on the chosen mode. Rationale: trusted-25 showed
        # the LLM often picks one valid mode but doesn't surface a second
        # plausible one — leaving alt_mode null suppresses hybrid even when
        # confidence says we're not certain.
        _AUTO_ALT_MIN_CONF = 0.6
        _AUTO_ALT_MAX_CONF = 0.75
        _AUTO_ALT_BY_MODE = {
            "mode_a": "mode_b",
            "mode_b": "mode_c",
            "mode_c": "mode_b",
        }
        if (
            alt_mode is None
            and _AUTO_ALT_MIN_CONF <= confidence < _AUTO_ALT_MAX_CONF
        ):
            candidate = _AUTO_ALT_BY_MODE.get(raw_mode)
            if candidate in allowed:
                alt_mode = candidate

        decision = {
            "mode": raw_mode,
            "confidence": confidence,
            "alt_mode": alt_mode,
            "rule_matched": None,
            "source": "llm",
            "reasoning": str(data.get("reasoning", ""))[:200],
        }
        router_cache.put(cache_key, decision)
        return _strip_hybrid_if_disabled(decision)
    except Exception as exc:
        logger.warning("routing_failed", error=str(exc))
        return _strip_hybrid_if_disabled(
            _default_decision("default", f"LLM routing failed: {exc}")
        )
