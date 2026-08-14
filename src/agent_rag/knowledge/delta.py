"""Pure transformation from changed-block extraction to a current knowledge delta."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from agent_rag.kg.extractor import PageExtractionResult
from agent_rag.kg.profiler import relation_id


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def entity_id(name: str, entity_type: str) -> str:
    # Keep compatibility with the original offline EntityResolver contract.
    # The type remains an alignment constraint, but is not part of the legacy ID.
    del entity_type
    return hashlib.md5(  # noqa: S324 - deterministic identifier, not security
        name.strip().lower().encode("utf-8"), usedforsecurity=False
    ).hexdigest()


class MentionDelta(BaseModel):
    entity_id: str
    entity_name: str
    entity_type: str
    description: str = ""
    block_id: str
    mention_form: str = ""


class FactDelta(BaseModel):
    fact_key: str
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    relation_type: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    weight: float = 1.0
    source_block_ids: list[str] = Field(default_factory=list)


class KnowledgeDelta(BaseModel):
    extraction_block_ids: list[str] = Field(default_factory=list)
    affected_old_block_ids: list[str] = Field(default_factory=list)
    relocated_blocks: dict[str, str] = Field(default_factory=dict)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[MentionDelta] = Field(default_factory=list)
    facts: list[FactDelta] = Field(default_factory=list)
    previous_facts: list[FactDelta] = Field(default_factory=list)
    retired_facts: list[FactDelta] = Field(default_factory=list)
    semantic_changed_fact_keys: list[str] = Field(default_factory=list)


def _resolve_entity(
    name: str,
    entity_type: str,
    existing_entities: list[dict[str, Any]],
) -> tuple[str, str]:
    wanted_name = _normalise(name).casefold()
    wanted_type = _normalise(entity_type).upper()
    matches: list[dict[str, Any]] = []
    for item in existing_entities:
        candidates = [item.get("entity_name", ""), *item.get("aliases", [])]
        if any(_normalise(candidate).casefold() == wanted_name for candidate in candidates):
            matches.append(item)
    typed = [
        item
        for item in matches
        if str(item.get("entity_type", "")).upper() == wanted_type
    ]
    selected = typed[0] if typed else (matches[0] if len(matches) == 1 else None)
    if selected is not None:
        return str(selected["entity_id"]), str(selected.get("entity_name") or name)
    return entity_id(name, wanted_type), _normalise(name)


def build_knowledge_delta(
    extraction: PageExtractionResult,
    *,
    extraction_block_ids: list[str],
    affected_old_block_ids: list[str],
    relocated_blocks: dict[str, str],
    existing_entities: list[dict[str, Any]],
) -> KnowledgeDelta:
    """Validate provenance/endpoints and produce deterministic IDs."""
    allowed_blocks = set(extraction_block_ids)
    resolved: dict[str, tuple[str, str, str, str]] = {}
    entities_by_id: dict[str, dict[str, Any]] = {}
    mentions: set[tuple[str, str, str]] = set()

    for entity in extraction.entities:
        refs = sorted(set(entity.source_block_refs) & allowed_blocks)
        if not refs or not _normalise(entity.name):
            continue
        eid, canonical = _resolve_entity(
            entity.name, entity.type, existing_entities
        )
        etype = _normalise(entity.type).upper() or "OTHER"
        resolved[_normalise(entity.name).casefold()] = (
            eid, canonical, etype, _normalise(entity.description)
        )
        if not any(str(item.get("entity_id")) == eid for item in existing_entities):
            entities_by_id[eid] = {
                "entity_id": eid,
                "entity_name": canonical,
                "entity_type": etype,
                "description": _normalise(entity.description),
                "aliases": [] if canonical == entity.name else [entity.name],
            }
        for block_id in refs:
            mentions.add((eid, block_id, _normalise(entity.name)))

    facts_by_key: dict[str, FactDelta] = {}
    for relation in extraction.relations:
        refs = sorted(set(relation.source_block_refs) & allowed_blocks)
        source = resolved.get(_normalise(relation.source).casefold())
        target = resolved.get(_normalise(relation.target).casefold())
        relation_type = re.sub(
            r"[^a-z0-9]+", "_", _normalise(relation.relation_type).casefold()
        ).strip("_")
        if not refs or source is None or target is None or not relation_type:
            continue
        if source[0] == target[0]:
            continue
        key = relation_id(source[0], target[0], relation_type)
        existing = facts_by_key.get(key)
        if existing:
            existing.source_block_ids = sorted(
                set(existing.source_block_ids) | set(refs)
            )
            continue
        facts_by_key[key] = FactDelta(
            fact_key=key,
            source_id=source[0],
            source_name=source[1],
            target_id=target[0],
            target_name=target[1],
            relation_type=relation_type,
            description=_normalise(relation.description),
            keywords=sorted(
                {
                    _normalise(item).casefold()
                    for item in relation.keywords
                    if _normalise(item)
                }
            ),
            weight=max(0.1, min(float(relation.strength) / 10.0, 1.0)),
            source_block_ids=refs,
        )

    entity_details = {
        item["entity_id"]: item for item in entities_by_id.values()
    }
    mention_models = []
    for eid, block_id, mention_form in sorted(mentions):
        details = entity_details.get(eid)
        if details is None:
            details = next(
                item for item in existing_entities if str(item.get("entity_id")) == eid
            )
        mention_models.append(
            MentionDelta(
                entity_id=eid,
                entity_name=str(details.get("entity_name", mention_form)),
                entity_type=str(details.get("entity_type", "OTHER")),
                description=str(details.get("description", "")),
                block_id=block_id,
                mention_form=mention_form,
            )
        )

    return KnowledgeDelta(
        extraction_block_ids=sorted(allowed_blocks),
        affected_old_block_ids=sorted(set(affected_old_block_ids)),
        relocated_blocks=dict(sorted(relocated_blocks.items())),
        entities=sorted(entities_by_id.values(), key=lambda item: item["entity_id"]),
        mentions=mention_models,
        facts=sorted(facts_by_key.values(), key=lambda item: item.fact_key),
    )


def merge_current_facts(
    delta: KnowledgeDelta,
    current_facts: list[FactDelta],
) -> KnowledgeDelta:
    """Merge extracted facts with remaining supports from affected current facts."""
    extracted = {fact.fact_key: fact.model_copy(deep=True) for fact in delta.facts}
    retired: dict[str, FactDelta] = {}
    semantic_changed: set[str] = set()
    affected = set(delta.affected_old_block_ids)

    for old in current_facts:
        remaining = []
        for block_id in old.source_block_ids:
            if block_id in delta.relocated_blocks:
                remaining.append(delta.relocated_blocks[block_id])
            elif block_id not in affected:
                remaining.append(block_id)
        incoming = extracted.get(old.fact_key)
        if incoming is not None:
            incoming.source_block_ids = sorted(
                set(incoming.source_block_ids) | set(remaining)
            )
            if (
                incoming.description != old.description
                or incoming.keywords != old.keywords
                or round(incoming.weight, 6) != round(old.weight, 6)
            ):
                semantic_changed.add(old.fact_key)
        elif remaining:
            kept = old.model_copy(deep=True)
            kept.source_block_ids = sorted(set(remaining))
            extracted[old.fact_key] = kept
        else:
            retired[old.fact_key] = old

    delta.previous_facts = sorted(
        (item.model_copy(deep=True) for item in current_facts),
        key=lambda item: item.fact_key,
    )
    delta.facts = sorted(extracted.values(), key=lambda item: item.fact_key)
    delta.retired_facts = sorted(retired.values(), key=lambda item: item.fact_key)
    delta.semantic_changed_fact_keys = sorted(semantic_changed)
    return delta
