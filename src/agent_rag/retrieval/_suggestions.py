"""Follow-up question generator.

Runs in parallel with answer generation on the cheap resolution-stage model.
Output is best-effort: any failure, timeout, or malformed JSON yields an empty
list and the SSE handler simply omits the `suggestions` event.

The frontend further sanitizes (length bounds, dedup, cap-at-3) before
rendering — see `frontend/components/SuggestionBubbles.tsx`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import json_repair
import structlog
from jinja2 import Template

from agent_rag.config import llm_config
from agent_rag.llm.client import AsyncLLMClient

logger = structlog.get_logger(__name__)

_TMPL = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_followups.j2")
    .read_text(encoding="utf-8")
)

_CHEAP_MODEL = (llm_config.get("resolution", {}) or {}).get("model")

# Soft timeout — the suggestion task is also cancelled by the SSE finally
# block on client disconnect, so this is a secondary backstop.
_TIMEOUT_S = 8.0

# Cap retrieved-evidence excerpt fed to the suggester. Keep the prompt small
# (one cheap-model call should land in ~1.3 s).
_MAX_BLOCKS = 3


async def generate_followups(
    query: str,
    blocks: list[dict[str, Any]],
    llm: AsyncLLMClient | None = None,
) -> list[str]:
    """Return up to 3 follow-up questions grounded in retrieved blocks.

    Best-effort: returns `[]` on any failure. Frontend treats `[]` as
    "no suggestions" and renders nothing.
    """
    if not blocks:
        return []

    trimmed = blocks[:_MAX_BLOCKS]
    prompt = _TMPL.render(query=query, blocks=trimmed)

    own_client = llm is None
    if llm is not None:
        client = llm
    elif _CHEAP_MODEL:
        client = AsyncLLMClient(model=_CHEAP_MODEL)
    else:
        client = AsyncLLMClient()

    try:
        try:
            raw = await asyncio.wait_for(
                client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4,
                    max_tokens=400,
                    response_format={"type": "json_object"},
                    use_cache=False,
                ),
                timeout=_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            # Propagate cancellation — the SSE finally block expects to
            # observe CancelledError when the user disconnects / hits Stop.
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("suggestions_call_failed", error=str(e))
            return []

        try:
            data = json_repair.loads(raw or "{}")
        except Exception as e:  # noqa: BLE001
            logger.warning("suggestions_parse_failed", error=str(e))
            return []

        items = data.get("suggestions", []) if isinstance(data, dict) else []
        cleaned: list[str] = []
        seen: set[str] = set()
        q_norm = query.strip().lower()
        for item in items:
            if not isinstance(item, str):
                continue
            s = item.strip().strip('"').strip("'")
            if not (8 <= len(s) <= 120):
                continue
            key = s.lower()
            if key == q_norm or key in seen:
                continue
            seen.add(key)
            cleaned.append(s)
            if len(cleaned) >= 3:
                break

        logger.info("suggestions_ok", emitted=len(cleaned))
        return cleaned
    finally:
        if own_client:
            await client.close()
