"""Bounded frontier arbitration when deterministic candidate scores are close."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import json_repair
from jinja2 import Template

from agent_rag.llm.client import LLMClient, cost_stage
from agent_rag.tools.schemas import FrontierSeed, QueryProfile

_TEMPLATE = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "select_frontier.j2")
    .read_text(encoding="utf-8")
)


class FrontierSelector:
    """Use rules for clear winners and constrained LLM arbitration for ties."""

    def __init__(
        self,
        llm_factory: Callable[[], LLMClient] = LLMClient,
        ambiguity_margin: float = 0.15,
        max_candidates: int = 50,
    ):
        self._llm_factory = llm_factory
        self.ambiguity_margin = ambiguity_margin
        self.max_candidates = max_candidates

    def select(
        self,
        query: str,
        candidates: list[FrontierSeed],
        profile: QueryProfile,
        missing_claims: list[str],
    ) -> tuple[FrontierSeed, str, str]:
        shortlist = candidates[: self.max_candidates]
        if len(shortlist) == 1:
            return shortlist[0], "rule", "Only one eligible frontier candidate."
        if shortlist[0].score - shortlist[1].score > self.ambiguity_margin:
            return shortlist[0], "rule", "Top deterministic score has a clear margin."

        llm = self._llm_factory()
        try:
            prompt = _TEMPLATE.render(
                query=query,
                missing_claims=missing_claims,
                constraints=[item.model_dump() for item in profile.constraints],
                candidates=[item.model_dump() for item in shortlist],
            )
            with cost_stage("frontier_selection"):
                raw = llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=256,
                    response_format={"type": "json_object"},
                    use_cache=True,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            data: Any = json_repair.loads(raw) or {}
            index = int(data.get("index", 0)) if isinstance(data, dict) else 0
            if index < 0 or index >= len(shortlist):
                raise ValueError("Frontier selector returned an invalid index")
            reason = str(data.get("reason", "Candidate selected by LLM.")).strip()
            return shortlist[index], "llm", reason[:300]
        except Exception:
            return shortlist[0], "fallback", "LLM arbitration failed; used top score."
        finally:
            llm.close()
