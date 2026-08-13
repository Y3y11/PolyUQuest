"""Open-domain query contract extraction for retrieval and evidence gating."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import json_repair
from jinja2 import Template

from agent_rag.config import llm_config
from agent_rag.llm.client import LLMClient, cost_stage
from agent_rag.tools.schemas import EvidenceRequirement, QueryConstraint, QueryProfile

_PROFILE_TEMPLATE = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "extract_query_profile.j2")
    .read_text(encoding="utf-8")
)
_PROFILE_CFG = llm_config.get("query_profile", {}) or {}
_PROFILE_MAX_TOKENS = int(_PROFILE_CFG.get("max_tokens", 768))

_PROCEDURE_SIGNALS = (
    "怎么",
    "如何",
    "步骤",
    "流程",
    "申请",
    "部署",
    "配置",
    "迁移",
    "how to",
    "procedure",
    "steps",
    "apply",
    "deploy",
    "configure",
    "migrate",
)


def _add_distinctive_atomic_aliases(constraint: QueryConstraint) -> QueryConstraint:
    """Preserve short identifiers inside compound labels (v4, COMP, PhD, iOS)."""
    aliases = list(constraint.aliases)
    for text in (constraint.label, constraint.value, *constraint.aliases):
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,11}", text):
            distinctive = (
                any(char.isdigit() for char in token)
                or token.isupper()
                or any(char.isupper() for char in token[1:])
            )
            if distinctive and token.casefold() not in {
                value.casefold() for value in aliases
            }:
                aliases.append(token)
    return constraint.model_copy(update={"aliases": aliases[:12]})


def build_query_profile(query: str) -> QueryProfile:
    """Build a domain-agnostic baseline; no institution ontology lives here."""
    lowered = query.lower()
    procedural = any(signal in lowered for signal in _PROCEDURE_SIGNALS)
    return QueryProfile(
        query=query,
        intents=["procedure"] if procedural else ["fact_lookup"],
        required_claims=[
            EvidenceRequirement(
                claim="actionable procedure" if procedural else "answer claim",
                evidence_cues=(
                    [
                        "how to",
                        "procedure",
                        "steps",
                        "instructions",
                        "requirements",
                        "submit",
                        "apply",
                        "configure",
                        "install",
                        "流程",
                        "步骤",
                        "申请",
                        "提交",
                    ]
                    if procedural
                    else []
                ),
            )
        ],
    )


class QueryProfileEnricher:
    """Resolve query semantics to a constrained JSON contract, cached by LLMClient."""

    def __init__(self, llm_factory: Callable[[], LLMClient] = LLMClient):
        self._llm_factory = llm_factory

    def enrich(self, profile: QueryProfile) -> QueryProfile:
        llm = self._llm_factory()
        try:
            with cost_stage("query_profile"):
                raw = llm.chat(
                    messages=[
                        {
                            "role": "user",
                            "content": _PROFILE_TEMPLATE.render(query=profile.query),
                        }
                    ],
                    temperature=0.0,
                    max_tokens=_PROFILE_MAX_TOKENS,
                    response_format={"type": "json_object"},
                    use_cache=True,
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except Exception:
            return profile.model_copy(update={"source": "fallback"})
        finally:
            llm.close()

        try:
            data: Any = json_repair.loads(raw) or {}
            if not isinstance(data, dict):
                return profile.model_copy(update={"source": "fallback"})
            constraints = [
                _add_distinctive_atomic_aliases(QueryConstraint.model_validate(item))
                for item in data.get("constraints", [])[:12]
                if isinstance(item, dict)
            ]
            intents = [
                str(value).strip()
                for value in data.get("intents", profile.intents)[:5]
                if str(value).strip()
            ]
            claims: list[EvidenceRequirement] = []
            for value in data.get("required_claims", [])[:8]:
                if isinstance(value, dict):
                    claims.append(EvidenceRequirement.model_validate(value))
                elif str(value).strip():
                    # Backward compatibility for cached responses produced by
                    # the original list[str] contract.
                    text = str(value).strip()
                    claims.append(EvidenceRequirement(claim=text, evidence_cues=[text]))
            return QueryProfile(
                query=profile.query,
                constraints=constraints,
                intents=intents or profile.intents,
                required_claims=claims or profile.required_claims,
                source="llm_enriched",
            )
        except Exception:
            return profile.model_copy(update={"source": "fallback"})


def profile_terms(profile: QueryProfile | None) -> list[str]:
    if profile is None:
        return []
    return [
        term
        for constraint in profile.constraints
        for term in [constraint.label, constraint.value, *constraint.aliases]
        if term and not re.fullmatch(r"[?\s]+", term)
    ]
