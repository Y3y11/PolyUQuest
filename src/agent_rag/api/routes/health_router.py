"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from agent_rag.api.schemas import HealthResponse
from agent_rag.config import settings

router = APIRouter()


def _dependency_status() -> tuple[bool, bool]:
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

    return neo4j_ok, qdrant_ok


@router.get("/health/live", response_model=HealthResponse)
def liveness():
    return HealthResponse(status="ok")


@router.get("/health/dependencies", response_model=HealthResponse)
def dependency_health():
    neo4j_ok, qdrant_ok = _dependency_status()
    service_status = "ok" if (neo4j_ok and qdrant_ok) else "degraded"
    return HealthResponse(
        status=service_status, neo4j=neo4j_ok, qdrant=qdrant_ok
    )


@router.get("/health", response_model=HealthResponse)
def health_check():
    """Backward-compatible alias for dependency health."""
    return dependency_health()


@router.get("/health/ready", response_model=HealthResponse)
def readiness(request: Request):
    neo4j_ok, qdrant_ok = _dependency_status()
    startup_complete = bool(getattr(request.app.state, "startup_complete", False))
    embedding_ok = bool(getattr(request.app.state, "embedding_ready", False))
    bm25_ok = bool(getattr(request.app.state, "bm25_ready", False))
    index_worker_ok = (
        not settings.index_worker_enabled
        or bool(
            getattr(
                getattr(request.app.state, "index_worker", None),
                "is_running",
                False,
            )
        )
    )
    ready = all(
        (
            startup_complete,
            embedding_ok,
            bm25_ok,
            neo4j_ok,
            qdrant_ok,
            index_worker_ok,
        )
    )
    payload = HealthResponse(
        status="ok" if ready else "not_ready",
        neo4j=neo4j_ok,
        qdrant=qdrant_ok,
        embedding=embedding_ok,
        bm25=bm25_ok,
        startup_complete=startup_complete,
        index_worker=index_worker_ok,
    )
    if ready:
        return payload
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )
