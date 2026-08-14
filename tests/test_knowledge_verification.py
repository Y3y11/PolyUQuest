from agent_rag.knowledge.delta import FactDelta, KnowledgeDelta
from agent_rag.storage.graph_vector_store import GraphVectorStore


class _Neo4j:
    def verify_incremental_knowledge(self, **_kwargs):
        return True


class _Qdrant:
    def __init__(self, source_block_ids: list[str]):
        self.source_block_ids = source_block_ids

    def retrieve_vectors(self, collection, ids):
        if collection == "relations" and ids == ["retired"]:
            return {}
        return {item: [1.0] for item in ids}

    def retrieve_payloads(self, _collection, ids):
        return {
            item: {"source_block_ids": self.source_block_ids} for item in ids
        }


def _store(source_block_ids: list[str]) -> GraphVectorStore:
    store = GraphVectorStore.__new__(GraphVectorStore)
    store.neo4j = _Neo4j()
    store.qdrant = _Qdrant(source_block_ids)
    return store


def _delta() -> KnowledgeDelta:
    fact = FactDelta(
        fact_key="current",
        source_id="a",
        source_name="A",
        target_id="b",
        target_name="B",
        relation_type="uses",
        source_block_ids=["block-new"],
    )
    retired = fact.model_copy(update={"fact_key": "retired"})
    return KnowledgeDelta(facts=[fact], retired_facts=[retired])


def test_knowledge_verification_accepts_matching_relation_payload() -> None:
    assert _store(["block-new"]).verify_incremental_knowledge(
        delta=_delta(), required_entity_ids=["a", "b"]
    )


def test_knowledge_verification_rejects_stale_relation_payload() -> None:
    assert not _store(["block-old"]).verify_incremental_knowledge(
        delta=_delta(), required_entity_ids=["a", "b"]
    )
