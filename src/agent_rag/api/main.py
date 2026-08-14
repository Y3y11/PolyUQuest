"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_rag.api.routes import (
    agent_router,
    freshness_router,
    graph_router,
    health_router,
    indexing_router,
    query_router,
    telemetry_router,
)
from agent_rag.config import settings
from agent_rag.retrieval import _bm25, _embedding

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.startup_complete = False
    app.state.embedding_ready = False
    app.state.bm25_ready = False
    app.state.index_worker = None
    app.state.freshness_worker = None
    # Warm BM25 in a background thread so it doesn't block startup. The cold
    # build is several minutes on a full corpus; after the first warm run we
    # persist to disk, so subsequent restarts are near-instant. If a query
    # lands before the warmup finishes, the module lock in _bm25 serializes
    # the two callers so we pay the wait once, not twice.
    def _warm() -> None:
        try:
            _bm25.warmup()
            app.state.bm25_ready = True
        except Exception as exc:
            logger.warning("bm25_warmup_failed", error=str(exc))
    threading.Thread(target=_warm, name="bm25-warmup", daemon=True).start()
    # Loading a local transformer can take ~20s. Treat it as readiness work so
    # the server accepts traffic only after query embedding is usable; otherwise
    # that cost appears unpredictably inside the first user's search action.
    try:
        await asyncio.to_thread(_embedding.warmup)
        app.state.embedding_ready = True
    except Exception as exc:
        logger.warning("embedding_warmup_failed", error=str(exc))
    if settings.agent_repair_on_startup:
        try:
            from agent_rag.tools.patch_recovery import recover_pending_patches

            report = await asyncio.to_thread(recover_pending_patches)
            logger.info(
                "patch_recovery_completed",
                scanned=report.scanned,
                recovered=report.recovered,
                skipped=report.skipped,
                failed=report.failed,
            )
        except Exception as exc:
            logger.warning("patch_recovery_scan_failed", error=str(exc))
    try:
        from agent_rag.freshness import page_lifecycle_store
        from agent_rag.storage.neo4j_store import Neo4jStore

        graph = Neo4jStore()
        try:
            indexed_pages = await asyncio.to_thread(graph.list_indexed_webpages)
            bootstrapped = await asyncio.to_thread(
                page_lifecycle_store.bootstrap_indexed_pages,
                indexed_pages,
            )
        finally:
            graph.close()
        if bootstrapped:
            logger.info("freshness_targets_bootstrapped", count=bootstrapped)
        from agent_rag.indexing.outbox import index_outbox

        jobs = {
            job.job_id: (job.status, job.last_error)
            for job in index_outbox.list(limit=10000)
        }
        lifecycle_recovery = await asyncio.to_thread(
            page_lifecycle_store.recover_stale_indexing, jobs
        )
        if any(lifecycle_recovery.values()):
            logger.info("freshness_lifecycle_recovered", **lifecycle_recovery)
    except Exception as exc:
        logger.warning("freshness_bootstrap_failed", error=str(exc))
    from agent_rag.freshness.worker import freshness_worker_lifespan
    from agent_rag.indexing.worker import index_worker_lifespan

    async with (
        index_worker_lifespan() as worker,
        freshness_worker_lifespan() as freshness_worker,
    ):
        app.state.index_worker = worker
        app.state.freshness_worker = freshness_worker
        app.state.startup_complete = True
        yield


app = FastAPI(
    title="Agent-RAG-PolyU",
    description="Structure-aware Graph-enhanced RAG for PolyU",
    version="0.1.0",
    lifespan=_lifespan,
)

_cors_kwargs: dict = {
    "allow_credentials": settings.cors_allow_credentials,
    "allow_methods": settings.cors_methods_list,
    "allow_headers": settings.cors_headers_list,
}
if settings.cors_allow_origin_regex:
    _cors_kwargs["allow_origin_regex"] = settings.cors_allow_origin_regex
    _cors_kwargs["allow_origins"] = []
else:
    _cors_kwargs["allow_origins"] = settings.cors_origins_list

app.add_middleware(CORSMiddleware, **_cors_kwargs)

app.include_router(query_router.router, prefix="/api", tags=["query"])
app.include_router(agent_router.router, prefix="/api", tags=["agent"])
app.include_router(graph_router.router, prefix="/api", tags=["graph"])
app.include_router(health_router.router, prefix="/api", tags=["health"])
app.include_router(indexing_router.router, prefix="/api", tags=["indexing"])
app.include_router(freshness_router.router, prefix="/api", tags=["freshness"])
app.include_router(telemetry_router.router, prefix="/api", tags=["telemetry"])


def start():
    import uvicorn
    uvicorn.run(
        "agent_rag.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
    )


if __name__ == "__main__":
    start()
