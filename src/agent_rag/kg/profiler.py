"""Key-Value Profiling + TopicKeyword extraction for entities and relations."""

from __future__ import annotations

import hashlib
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def entity_to_kv(entity: dict[str, Any]) -> dict[str, str]:
    """Generate Key-Value pair for an entity (for embedding and indexing)."""
    key = entity["entity_name"]
    value = entity.get("description", "")
    return {"key": key, "value": value, "text_for_embedding": f"{key}: {value}"}


def relation_to_kv(relation: dict[str, Any]) -> dict[str, str]:
    """Generate Key-Value pair for a relation edge."""
    src = relation.get("source", "")
    tgt = relation.get("target", "")
    desc = relation.get("description", "")
    keywords = relation.get("keywords", [])
    kw_str = ", ".join(keywords)
    key = f"{src} -> {tgt} ({relation.get('relation_type', '')})"
    value = f"{desc}. Topics: {kw_str}" if kw_str else desc
    return {"key": key, "value": value, "text_for_embedding": f"{key}: {value}"}


def relation_id(source_id: str, target_id: str, relation_type: str) -> str:
    raw = f"{source_id}|{target_id}|{relation_type}"
    return hashlib.md5(raw.encode()).hexdigest()


def collect_topic_keywords(relations: list[dict[str, Any]]) -> set[str]:
    """Collect all unique high-level topic keywords from extracted relations."""
    keywords: set[str] = set()
    for rel in relations:
        for kw in rel.get("keywords", []):
            kw = kw.strip().lower()
            if kw and len(kw) > 2:
                keywords.add(kw)
    return keywords
