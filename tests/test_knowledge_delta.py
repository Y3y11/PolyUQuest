from __future__ import annotations

from agent_rag.kg.extractor import (
    ExtractedEntityPage,
    ExtractedRelationPage,
    PageExtractionResult,
)
from agent_rag.knowledge import FactDelta, build_knowledge_delta, merge_current_facts
from agent_rag.knowledge.delta import entity_id


def _extraction() -> PageExtractionResult:
    return PageExtractionResult(
        entities=[
            ExtractedEntityPage(
                name="Acme Platform",
                type="PRODUCT",
                description="Enterprise platform",
                source_block_refs=["new-block"],
            ),
            ExtractedEntityPage(
                name="SSO Service",
                type="SERVICE",
                description="Sign-in service",
                source_block_refs=["new-block"],
            ),
        ],
        relations=[
            ExtractedRelationPage(
                source="Acme Platform",
                target="SSO Service",
                relation_type="requires",
                description="Acme Platform requires SSO Service",
                strength=9,
                keywords=["Identity"],
                source_block_refs=["new-block", "not-allowed"],
            )
        ],
    )


def test_delta_is_open_domain_and_filters_invalid_provenance() -> None:
    delta = build_knowledge_delta(
        _extraction(),
        extraction_block_ids=["new-block"],
        affected_old_block_ids=["old-block"],
        relocated_blocks={},
        existing_entities=[],
    )
    assert {item["entity_type"] for item in delta.entities} == {
        "PRODUCT", "SERVICE"
    }
    assert delta.facts[0].source_block_ids == ["new-block"]
    assert delta.facts[0].relation_type == "requires"
    assert delta.facts[0].keywords == ["identity"]


def test_existing_entity_exact_alias_is_reused() -> None:
    existing = [{
        "entity_id": "existing-id",
        "entity_name": "Acme Platform",
        "entity_type": "PRODUCT",
        "description": "Canonical profile",
        "aliases": ["Acme"],
    }]
    extraction = _extraction()
    extraction.entities[0].name = "Acme"
    extraction.relations[0].source = "Acme"
    delta = build_knowledge_delta(
        extraction,
        extraction_block_ids=["new-block"],
        affected_old_block_ids=[],
        relocated_blocks={},
        existing_entities=existing,
    )
    acme_mention = next(
        item for item in delta.mentions if item.mention_form == "Acme"
    )
    assert acme_mention.entity_id == "existing-id"
    assert all(item["entity_id"] != "existing-id" for item in delta.entities)


def test_last_support_retires_fact_but_remaining_support_keeps_it() -> None:
    source_id = entity_id("Acme Platform", "PRODUCT")
    target_id = entity_id("SSO Service", "SERVICE")
    old = FactDelta(
        fact_key="fact-1",
        source_id=source_id,
        source_name="Acme Platform",
        target_id=target_id,
        target_name="SSO Service",
        relation_type="requires",
        source_block_ids=["old-block", "stable-block"],
    )
    empty = build_knowledge_delta(
        PageExtractionResult(),
        extraction_block_ids=[],
        affected_old_block_ids=["old-block"],
        relocated_blocks={},
        existing_entities=[],
    )
    kept = merge_current_facts(empty, [old])
    assert kept.facts[0].source_block_ids == ["stable-block"]
    assert kept.retired_facts == []

    next_delta = build_knowledge_delta(
        PageExtractionResult(),
        extraction_block_ids=[],
        affected_old_block_ids=["stable-block"],
        relocated_blocks={},
        existing_entities=[],
    )
    retired = merge_current_facts(next_delta, kept.facts)
    assert retired.facts == []
    assert retired.retired_facts[0].fact_key == "fact-1"


def test_relocation_moves_support_without_extraction() -> None:
    old = FactDelta(
        fact_key="fact-1",
        source_id="a",
        source_name="A",
        target_id="b",
        target_name="B",
        relation_type="uses",
        source_block_ids=["old-id"],
    )
    delta = build_knowledge_delta(
        PageExtractionResult(),
        extraction_block_ids=[],
        affected_old_block_ids=["old-id"],
        relocated_blocks={"old-id": "new-id"},
        existing_entities=[],
    )
    merged = merge_current_facts(delta, [old])
    assert merged.facts[0].source_block_ids == ["new-id"]
    assert merged.retired_facts == []


def test_new_extraction_preserves_stable_support_from_existing_fact() -> None:
    extracted = build_knowledge_delta(
        _extraction(),
        extraction_block_ids=["new-block"],
        affected_old_block_ids=["old-block"],
        relocated_blocks={},
        existing_entities=[],
    )
    fact = extracted.facts[0]
    old = fact.model_copy(
        update={
            "description": "old description",
            "source_block_ids": ["old-block", "stable-block"],
        }
    )
    merged = merge_current_facts(extracted, [old])
    assert merged.facts[0].source_block_ids == ["new-block", "stable-block"]
    assert merged.semantic_changed_fact_keys == [fact.fact_key]
