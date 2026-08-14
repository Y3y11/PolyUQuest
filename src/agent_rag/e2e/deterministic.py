"""Deterministic replacements for non-repeatable model boundaries in E2E."""

from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Any

from agent_rag.kg.extractor import (
    ExtractedEntityPage,
    ExtractedRelationPage,
    PageExtractionResult,
)
from agent_rag.tools.schemas import EvidenceBlock, QueryProfile

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*|[\u3400-\u9fff]", re.IGNORECASE)


class DeterministicEmbedder:
    """Stable hashing projection shared by document and query paths."""

    def __init__(self, dimension: int):
        if dimension < 8:
            raise ValueError("Deterministic embedding dimension must be at least 8")
        self.dimension = dimension
        self.batch_calls = 0
        self.query_calls = 0
        self.texts_embedded = 0
        self._lock = threading.Lock()

    def _embed(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        tokens = _TOKEN.findall(text.casefold())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            values[index] += sign
        if not tokens:
            values[0] = 1.0
        norm = math.sqrt(sum(item * item for item in values)) or 1.0
        return [round(item / norm, 8) for item in values]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.batch_calls += 1
            self.texts_embedded += len(texts)
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            self.query_calls += 1
        return self._embed(text)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "batch_calls": self.batch_calls,
                "query_calls": self.query_calls,
                "texts_embedded": self.texts_embedded,
            }


class DeterministicLLM:
    """Minimal client used only where a production constructor requires one."""

    def chat(self, **_kwargs: Any) -> str:
        return '{"index": 0, "reason": "deterministic E2E candidate"}'

    def close(self) -> None:
        return None


class DeterministicProfileEnricher:
    def enrich(self, profile: QueryProfile) -> QueryProfile:
        return profile


class DeterministicAnswerComposer:
    def compose(
        self,
        _query: str,
        evidence: list[EvidenceBlock],
        _history: list[dict[str, str]],
    ) -> str:
        if not evidence:
            return "No supported answer."
        sources = list(dict.fromkeys(item.source_url for item in evidence))
        best = max(
            evidence,
            key=lambda item: (
                "step" in item.content.casefold(),
                item.scores.reranker or item.scores.retrieval,
            ),
        )
        return f"{best.content}\n\nSources: {'; '.join(sources)}"


class DeterministicKnowledgeExtractor:
    """Emit strict entity/relation structures from the controlled fixture."""

    def __init__(self, token: str):
        self.token = token
        self.calls = 0

    def __call__(
        self,
        _url: str,
        blocks: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> PageExtractionResult:
        self.calls += 1
        candidates = [
            block
            for block in blocks
            if "approval" in str(block.get("content", "")).casefold()
        ]
        procedure = (
            max(
                candidates,
                key=lambda block: (
                    "step 1" in str(block.get("content", "")).casefold(),
                    str(block.get("heading_context", "")).count(">"),
                    -len(str(block.get("content", ""))),
                ),
            )
            if candidates
            else None
        )
        if procedure is None:
            return PageExtractionResult()
        text = str(procedure.get("content", ""))
        source = f"Access Portal {self.token}"
        target = (
            f"Security Review Board {self.token}"
            if "Security Review Board" in text
            else f"Platform Team {self.token}"
        )
        block_id = str(procedure["block_id"])
        return PageExtractionResult(
            entities=[
                ExtractedEntityPage(
                    name=source,
                    type="SERVICE",
                    description="Controlled access request service.",
                    source_block_refs=[block_id],
                ),
                ExtractedEntityPage(
                    name=target,
                    type="ORGANIZATION",
                    description="Approval owner for the controlled request.",
                    source_block_refs=[block_id],
                ),
            ],
            relations=[
                ExtractedRelationPage(
                    source=source,
                    target=target,
                    relation_type="requires_approval_from",
                    description=f"{source} requires approval from {target}.",
                    strength=9,
                    keywords=["approval", "database access"],
                    source_block_refs=[block_id],
                )
            ],
        )
