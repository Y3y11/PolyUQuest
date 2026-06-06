"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from agent_rag.api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health_check():
    neo4j_ok = False
    qdrant_ok = False

    try:
        from agent_rag.storage.neo4j_store import Neo4jStore
        store = Neo4jStore()
        try:
            store.get_graph_stats()
            neo4j_ok = True
        finally:
            store.close()
    except Exception:
        pass

    try:
        from agent_rag.storage.qdrant_store import QdrantStore
        qs = QdrantStore()
        try:
            qs._client.get_collections()
            qdrant_ok = True
        finally:
            qs.close()
    except Exception:
        pass

    status = "ok" if (neo4j_ok and qdrant_ok) else "degraded"
    return HealthResponse(status=status, neo4j=neo4j_ok, qdrant=qdrant_ok)
