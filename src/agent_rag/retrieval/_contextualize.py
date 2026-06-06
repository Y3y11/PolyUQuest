"""Retrieval-side query contextualizer.

Rewrites a follow-up user query into a self-contained search query using
the last few conversation turns. The output feeds the *retrieval* path only
(embedding + keyword/entity expansion). The router still sees the raw query
and the answer prompt still sees the raw query + history — by design.

Skipped when `history` is empty, so first-turn latency is unchanged. Uses
the cheap resolution-stage model (~1.3 s/call) and bypasses cache because
inputs are short-tailed and hit rate would be near zero.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from jinja2 import Template

from agent_rag.api.schemas import Turn
from agent_rag.config import llm_config
from agent_rag.llm.client import AsyncLLMClient

logger = structlog.get_logger(__name__)

_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "contextualize_query.j2")
    .read_text(encoding="utf-8")
)

# Reuse the resolution-stage cheap model (Ling-flash-2.0). The contextualizer
# has the same shape as the resolver: tiny input, tiny output, JSON-free, must
# be fast on the user's critical path.
_CHEAP_MODEL = (llm_config.get("resolution", {}) or {}).get("model")

# Hard upper bound — if Ling-flash-2.0 hiccups we'd rather fall back to the
# raw query than block retrieval on the user's first follow-up.
_TIMEOUT_S = 5.0


async def contextualize_query(
    query: str,
    history: list[Turn] | None,
    llm: AsyncLLMClient | None = None,
) -> str:
    """Return a standalone retrieval query.

    If ``history`` is empty/None, returns ``query`` unchanged with zero LLM cost.
    On any failure (timeout, malformed output, transport error), falls back to
    the raw query — contextualization is best-effort, never blocking.

    ``llm`` is optional; if not provided, a one-shot AsyncLLMClient is opened
    against the cheap model. Callers that already have a cheap-model client
    open (e.g. the suggestion task) can pass it in to share the connection.
    """
    if not history:
        return query

    # Trim to the last 3 pairs; the prompt template is already small but
    # caller might pass a longer log.
    recent = history[-3:]

    prompt = _TMPL.render(query=query, history=[
        {"user": t.user, "assistant": t.assistant} for t in recent
    ])

    own_client = llm is None
    if llm is not None:
        client = llm
    elif _CHEAP_MODEL:
        client = AsyncLLMClient(model=_CHEAP_MODEL)
    else:
        client = AsyncLLMClient()
    try:
        try:
            rewritten = await asyncio.wait_for(
                client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=120,
                    use_cache=False,
                ),
                timeout=_TIMEOUT_S,
            )
        except (TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("contextualize_failed", error=str(e))
            return query

        cleaned = (rewritten or "").strip().strip('"').strip("'")
        # Strip a leading "Standalone search query:" if the model echoes the header.
        for prefix in ("Standalone search query:", "Search query:", "Query:"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix):].strip()

        # Guardrails: empty / too-long / model returned the literal prompt.
        if not cleaned or len(cleaned) > 300:
            return query
        if cleaned.lower() == query.lower():
            return query

        logger.info(
            "contextualize_ok",
            raw_len=len(query),
            rewritten_len=len(cleaned),
            history_turns=len(recent),
        )
        return cleaned
    finally:
        if own_client:
            await client.close()
