from types import SimpleNamespace

from agent_rag.kg.profiler import relation_id
from agent_rag.storage.graph_vector_store import GraphVectorStore
from agent_rag.storage.qdrant_store import QdrantStore


def test_qdrant_relation_inventory_derives_legacy_fact_key() -> None:
    expected = relation_id("entity-a", "entity-b", "uses")

    class Client:
        def scroll(self, **_kwargs):
            return (
                [
                    SimpleNamespace(
                        id=1,
                        payload={
                            "source_id": "entity-a",
                            "target_id": "entity-b",
                            "relation_type": "uses",
                            "source_block_ids": ["block-1"],
                        },
                    )
                ],
                None,
            )

    store = QdrantStore.__new__(QdrantStore)
    store._client = Client()  # noqa: SLF001

    payloads = store.get_all_payloads("relations")

    assert payloads[expected]["source_block_ids"] == ["block-1"]


def test_inventory_recovers_full_patch_id_from_agent_build_id() -> None:
    class Neo4j:
        def get_reconciliation_snapshot(self):
            return {
                "objects": {
                    "webpages": {
                        "https://example.org": {
                            "build_id": "agent:run-123:patch-full-id",
                            "patch_id": "",
                            "source_url": "https://example.org",
                            "vector_expected": False,
                        }
                    },
                    "blocks": {},
                    "entities": {},
                },
                "facts": {},
            }

    class Qdrant:
        def get_all_payloads(self, _collection):
            return {}

    store = GraphVectorStore.__new__(GraphVectorStore)
    store.neo4j = Neo4j()
    store.qdrant = Qdrant()

    inventory = store.collect_reconciliation_inventory()

    assert inventory.neo4j_patch_ids["https://example.org"]["patch_id"] == (
        "patch-full-id"
    )
    assert "https://example.org" in inventory.non_vector_webpage_ids
