"""Deterministic, open-domain quality gate for fetched web pages."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from agent_rag.config import agent_config
from agent_rag.tools.observations import ObservationRecord
from agent_rag.tools.schemas import FetchOutput

QualityAction = Literal["index", "evidence_only", "discard"]


class PageQualityFeatures(BaseModel):
    block_count: int = 0
    relevant_block_count: int = 0
    total_text_chars: int = 0
    total_tokens: int = 0
    text_html_ratio: float = 0.0
    link_count: int = 0
    links_per_1k_chars: float = 0.0
    duplicate_block_ratio: float = 0.0
    largest_block_share: float = 0.0
    query_coverage: float = 0.0
    title_present: bool = False
    content_hash_present: bool = False


class PageQualityDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"quality-{uuid.uuid4().hex}")
    observation_id: str
    run_id: str
    source_url: str
    content_hash: str
    action: QualityAction
    evidence_usable: bool
    score: float = Field(ge=0.0, le=1.0)
    policy_version: str
    reasons: list[str] = Field(default_factory=list)
    features: PageQualityFeatures


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


class PageQualityGate:
    """Score observations using generic structural and retrieval features."""

    def __init__(self, config: dict | None = None):
        self.config = config or dict(agent_config.get("quality", {}))
        self.policy_version = str(self.config.get("policy_version", "page-quality-v1"))

    def evaluate(
        self,
        observation: ObservationRecord,
        fetched: FetchOutput,
        *,
        require_query_relevance: bool = True,
    ) -> PageQualityDecision:
        cfg = self.config
        contents = [
            _normalise_text(str(block.get("content", "")))
            for block in observation.blocks
        ]
        contents = [content for content in contents if content]
        lengths = [len(content) for content in contents]
        total_chars = sum(lengths)
        counts = Counter(contents)
        duplicate_blocks = sum(count - 1 for count in counts.values() if count > 1)
        link_count = len(observation.discovered_links)
        relevant_blocks = (
            fetched.evidence_gain.relevant_blocks
            if require_query_relevance
            else len(observation.blocks)
        )
        query_coverage = (
            fetched.evidence_gain.lexical_coverage
            if require_query_relevance
            else float(cfg.get("target_query_coverage", 0.35))
        )
        features = PageQualityFeatures(
            block_count=len(observation.blocks),
            relevant_block_count=relevant_blocks,
            total_text_chars=total_chars,
            total_tokens=sum(
                int(block.get("token_count", 0) or 0)
                or max(1, len(str(block.get("content", ""))) // 4)
                for block in observation.blocks
                if str(block.get("content", "")).strip()
            ),
            text_html_ratio=round(
                min(total_chars / max(len(observation.raw_html), 1), 1.0), 4
            ),
            link_count=link_count,
            links_per_1k_chars=round(link_count * 1000 / max(total_chars, 1), 4),
            duplicate_block_ratio=round(duplicate_blocks / max(len(contents), 1), 4),
            largest_block_share=round(max(lengths, default=0) / max(total_chars, 1), 4),
            query_coverage=query_coverage,
            title_present=bool(str(observation.metadata.get("title", "")).strip()),
            content_hash_present=bool(
                str(observation.metadata.get("content_hash", "")).strip()
            ),
        )

        hard_reasons: list[str] = []
        if not str(observation.metadata.get("url", "")).strip():
            hard_reasons.append("missing_source_url")
        if not features.content_hash_present:
            hard_reasons.append("missing_content_hash")
        if features.relevant_block_count <= 0:
            hard_reasons.append("no_query_relevant_blocks")
        if features.total_text_chars < int(cfg.get("min_usable_chars", 40)):
            hard_reasons.append("insufficient_substantive_text")
        if hard_reasons:
            return self._decision(observation, features, "discard", False, 0.0, hard_reasons)

        weights = cfg.get("weights", {}) or {}
        min_index_chars = max(int(cfg.get("min_index_chars", 400)), 1)
        min_index_blocks = max(int(cfg.get("min_index_blocks", 2)), 1)
        target_text_ratio = max(float(cfg.get("target_text_html_ratio", 0.12)), 0.001)
        max_links_density = max(float(cfg.get("max_links_per_1k_chars", 20.0)), 0.001)
        max_duplicate_ratio = max(float(cfg.get("max_duplicate_block_ratio", 0.45)), 0.001)
        target_query_coverage = max(float(cfg.get("target_query_coverage", 0.35)), 0.001)

        components = {
            "text_volume": min(features.total_text_chars / min_index_chars, 1.0),
            "block_structure": min(features.block_count / min_index_blocks, 1.0),
            "text_density": min(features.text_html_ratio / target_text_ratio, 1.0),
            "link_density": max(0.0, 1.0 - features.links_per_1k_chars / max_links_density),
            "uniqueness": max(0.0, 1.0 - features.duplicate_block_ratio / max_duplicate_ratio),
            "query_relevance": min(features.query_coverage / target_query_coverage, 1.0),
            "metadata": (float(features.title_present) + float(features.content_hash_present)) / 2,
        }
        default_weights = {
            "text_volume": 0.22,
            "block_structure": 0.12,
            "text_density": 0.12,
            "link_density": 0.12,
            "uniqueness": 0.12,
            "query_relevance": 0.22,
            "metadata": 0.08,
        }
        resolved_weights = {
            name: float(weights.get(name, default)) for name, default in default_weights.items()
        }
        total_weight = sum(resolved_weights.values()) or 1.0
        weighted_score = sum(
            components[name] * weight for name, weight in resolved_weights.items()
        )
        score = weighted_score / total_weight
        score = round(max(0.0, min(score, 1.0)), 4)

        reasons: list[str] = []
        index_blockers: list[str] = []
        if features.total_text_chars < min_index_chars:
            reasons.append("thin_but_usable_content")
            index_blockers.append("thin_but_usable_content")
        if features.block_count < min_index_blocks:
            reasons.append("limited_structural_coverage")
            index_blockers.append("limited_structural_coverage")
        if features.links_per_1k_chars > max_links_density:
            reasons.append("navigation_heavy_page")
        if features.duplicate_block_ratio > max_duplicate_ratio:
            reasons.append("high_duplicate_content_ratio")
        if features.text_html_ratio < float(cfg.get("min_index_text_html_ratio", 0.025)):
            reasons.append("low_text_to_html_ratio")
            index_blockers.append("low_text_to_html_ratio")
        if features.query_coverage < float(cfg.get("min_index_query_coverage", 0.08)):
            reasons.append("weak_query_relevance")
            index_blockers.append("weak_query_relevance")
        if not features.title_present:
            reasons.append("missing_title")
            index_blockers.append("missing_title")

        index_threshold = float(cfg.get("index_score_threshold", 0.58))
        action: QualityAction = (
            "index" if score >= index_threshold and not index_blockers else "evidence_only"
        )
        if action == "index":
            reasons.insert(0, "quality_threshold_passed")
        elif not reasons:
            reasons = ["quality_score_below_index_threshold"]
        return self._decision(observation, features, action, True, score, reasons)

    def fallback(self, observation: ObservationRecord, error: Exception) -> PageQualityDecision:
        return self._decision(
            observation,
            PageQualityFeatures(
                block_count=len(observation.blocks),
                total_text_chars=sum(
                    len(str(block.get("content", ""))) for block in observation.blocks
                ),
                title_present=bool(observation.metadata.get("title")),
                content_hash_present=bool(observation.metadata.get("content_hash")),
            ),
            "evidence_only",
            True,
            0.0,
            [f"quality_gate_error:{type(error).__name__}"],
        )

    def _decision(
        self,
        observation: ObservationRecord,
        features: PageQualityFeatures,
        action: QualityAction,
        evidence_usable: bool,
        score: float,
        reasons: list[str],
    ) -> PageQualityDecision:
        return PageQualityDecision(
            observation_id=observation.observation_id,
            run_id=observation.run_id,
            source_url=str(observation.metadata.get("url", "")),
            content_hash=str(observation.metadata.get("content_hash", "")),
            action=action,
            evidence_usable=evidence_usable,
            score=score,
            policy_version=self.policy_version,
            reasons=reasons,
            features=features,
        )
