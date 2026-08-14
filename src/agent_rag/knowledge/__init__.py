"""Incremental current knowledge and bitemporal fact history."""

from agent_rag.knowledge.delta import (
    FactDelta,
    KnowledgeDelta,
    MentionDelta,
    build_knowledge_delta,
    merge_current_facts,
)
from agent_rag.knowledge.store import FactVersion, FactVersionStore, fact_version_store

__all__ = [
    "FactDelta",
    "FactVersion",
    "FactVersionStore",
    "KnowledgeDelta",
    "MentionDelta",
    "build_knowledge_delta",
    "merge_current_facts",
    "fact_version_store",
]
