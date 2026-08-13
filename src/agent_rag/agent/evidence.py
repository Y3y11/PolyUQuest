"""Deterministic evidence-gap evaluator used by the bounded MVP controller."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from agent_rag.agent.query_profile import build_query_profile
from agent_rag.config import agent_config
from agent_rag.tools._ranking import (
    constraints_supported,
    lexical_score,
    requirement_status,
)
from agent_rag.tools.schemas import EvidenceBlock, QueryProfile


class EvidenceAssessment(BaseModel):
    decision: Literal["answer", "expand", "refresh", "abstain"]
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    supported_claims: list[str] = Field(default_factory=list)
    missing_claims: list[str] = Field(default_factory=list)


_FRESHNESS_TERMS = {
    "latest",
    "current",
    "today",
    "now",
    "deadline",
    "最新",
    "目前",
    "现在",
    "今年",
    "截止",
}

_PROCEDURE_MARKERS = (
    "how to", "procedure", "steps", "step 1", "step one", "instructions",
    "requirements", "prerequisite", "before you", "submit", "upload",
    "install", "configure", "set up", "setup", "run the", "click",
    "navigate to", "流程", "步骤", "操作指南", "申请程序", "提交", "上传",
    "安装", "配置", "设置",
)

def is_freshness_sensitive(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in _FRESHNESS_TERMS)


def _supports_intent(profile: QueryProfile, content: str) -> bool:
    """Require operational evidence for any open-domain procedure query."""
    if "procedure" not in profile.intents:
        return True
    lowered = content.lower()
    marker_hits = sum(marker in lowered for marker in _PROCEDURE_MARKERS)
    enumerated_steps = bool(re.search(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+", content))
    return marker_hits >= 1 or enumerated_steps


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


class EvidenceEvaluator:
    def __init__(self):
        cfg = agent_config.get("evidence", {})
        self.min_lexical = float(cfg.get("min_lexical_coverage", 0.18))
        self.min_dense = float(cfg.get("min_dense_score", 0.42))
        self.min_reranker = float(cfg.get("min_reranker_score", 0.20))
        self.min_answer_confidence = float(cfg.get("min_answer_confidence", 0.30))
        self.fresh_ttl = timedelta(hours=int(cfg.get("fresh_ttl_hours", 168)))

    def assess(
        self,
        query: str,
        evidence: list[EvidenceBlock],
        freshness: str = "auto",
        can_explore: bool = True,
        query_profile: QueryProfile | None = None,
    ) -> EvidenceAssessment:
        query_profile = query_profile or build_query_profile(query)
        if not evidence:
            missing_claims = [
                item.claim for item in query_profile.required_claims if item.required
            ]
            return EvidenceAssessment(
                decision="expand" if can_explore else "abstain",
                reasons=["No evidence block was retrieved."],
                missing_claims=missing_claims,
            )

        ranked_confidence: list[float] = []
        relevant_detail_texts: list[str] = []
        detail_mismatch = False
        for block in evidence:
            evidence_text = " ".join(
                (
                    block.source_url,
                    block.source_title,
                    block.heading_context,
                    block.content,
                )
            )
            lexical = lexical_score(query, evidence_text)
            score = block.scores
            relevance_supported = (
                lexical >= self.min_lexical
                or score.retrieval >= self.min_dense
                or (score.reranker is not None and score.reranker >= self.min_reranker)
            )
            detail_supported = _supports_intent(query_profile, block.content)
            if relevance_supported and not detail_supported:
                detail_mismatch = True
            if relevance_supported and detail_supported:
                relevant_detail_texts.append(evidence_text)
                ranked_confidence.append(
                    max(lexical, score.retrieval, score.reranker or 0.0)
                )

        # Web evidence is inherently compositional: a parent page can establish
        # scope while a child page supplies the procedure. Evaluate the explicit
        # constraints and required claims over the relevant evidence set, rather
        # than requiring every fact to repeat inside one DOM block.
        evidence_corpus = "\n".join(relevant_detail_texts)
        entity_supported = constraints_supported(
            query_profile, evidence_corpus, {"entity"}
        )
        qualifier_supported = constraints_supported(
            query_profile, evidence_corpus, {"qualifier"}
        )
        supported_claims, missing_claims = requirement_status(
            query_profile, evidence_corpus
        )

        freshness_required = freshness == "require_fresh" or (
            freshness == "auto" and is_freshness_sensitive(query)
        )
        if freshness_required:
            cutoff = datetime.now(UTC) - self.fresh_ttl
            fresh_support = any(
                (stamp := _parse_timestamp(block.fetched_at)) is not None and stamp >= cutoff
                for block in evidence
            )
            if not fresh_support:
                return EvidenceAssessment(
                    decision="refresh" if can_explore else "abstain",
                    confidence=max(ranked_confidence, default=0.0),
                    reasons=["The question is freshness-sensitive but indexed evidence is stale."],
                    supported_claims=supported_claims,
                    missing_claims=missing_claims,
                )

        best_confidence = min(1.0, max(ranked_confidence, default=0.0))
        if (
            ranked_confidence
            and entity_supported
            and qualifier_supported
            and not missing_claims
            and best_confidence >= self.min_answer_confidence
        ):
            return EvidenceAssessment(
                decision="answer",
                confidence=best_confidence,
                reasons=["At least one evidence block passes relevance checks."],
                supported_claims=supported_claims,
            )
        return EvidenceAssessment(
            decision="expand" if can_explore else "abstain",
            confidence=best_confidence,
            reasons=[
                "Required answer claims remain unsupported."
                if missing_claims and ranked_confidence
                else (
                    "Retrieved blocks conflict with an explicit query qualifier."
                    if ranked_confidence and not qualifier_supported
                    else (
                        "Retrieved blocks do not preserve the requested target entity."
                        if ranked_confidence and not entity_supported
                        else (
                            "Relevant evidence confidence is below the answer threshold."
                            if ranked_confidence
                            else (
                                (
                                    "Retrieved blocks match navigation context but not "
                                    "the requested details."
                                )
                                if detail_mismatch
                                else "Retrieved blocks do not pass relevance checks."
                            )
                        )
                    )
                )
            ],
            supported_claims=supported_claims,
            missing_claims=missing_claims,
        )
