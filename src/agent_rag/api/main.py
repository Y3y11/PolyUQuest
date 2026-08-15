"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_rag.api.routes import (
    agent_router,
    freshness_router,
    graph_router,
    health_router,
    indexing_router,
    metrics_router,
    query_router,
    security_router,
    telemetry_router,
    worker_router,
)
from agent_rag.config import settings
from agent_rag.retrieval import _bm25, _embedding
from agent_rag.security.audit import SecurityAuditMiddleware
from agent_rag.security.store import security_audit_store
from agent_rag.tracing import trace_runtime

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _tracing_lifespan():
    try:
        yield
    finally:
        await asyncio.to_thread(trace_runtime.shutdown)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if settings.app_environment == "production" and settings.app_process_role != "api":
        raise RuntimeError("FastAPI production process requires APP_PROCESS_ROLE=api")
    app.state.startup_complete = False
    app.state.embedding_ready = False
    app.state.bm25_ready = False
    app.state.index_worker = None
    app.state.freshness_worker = None
    app.state.agent_run_worker = None
    from agent_rag.runtime import prepare_process_runtime

    await asyncio.to_thread(prepare_process_runtime)
    try:
        cutoff = datetime.now(UTC) - timedelta(
            days=settings.security_audit_retention_days
        )
        purged = await asyncio.to_thread(security_audit_store.purge, cutoff.isoformat())
        if purged:
            logger.info("security_audit_purged", deleted=purged)
    except Exception as exc:
        logger.warning(
            "security_audit_purge_failed", error_type=type(exc).__name__
        )
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
    if (
        settings.agent_repair_on_startup
        or settings.index_worker_enabled
        or settings.freshness_worker_enabled
    ):
        from agent_rag.workers.bootstrap import bootstrap_background_state

        await bootstrap_background_state()
    from agent_rag.freshness.worker import freshness_worker_lifespan
    from agent_rag.indexing.worker import index_worker_lifespan
    from agent_rag.runs.worker import agent_run_worker_lifespan

    async with (
        _tracing_lifespan(),
        agent_run_worker_lifespan() as agent_run_worker,
        index_worker_lifespan() as worker,
        freshness_worker_lifespan() as freshness_worker,
    ):
        app.state.agent_run_worker = agent_run_worker
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
app.add_middleware(SecurityAuditMiddleware)

app.include_router(query_router.router, prefix="/api", tags=["query"])
app.include_router(agent_router.router, prefix="/api", tags=["agent"])
app.include_router(graph_router.router, prefix="/api", tags=["graph"])
app.include_router(health_router.router, prefix="/api", tags=["health"])
app.include_router(indexing_router.router, prefix="/api", tags=["indexing"])
app.include_router(freshness_router.router, prefix="/api", tags=["freshness"])
app.include_router(telemetry_router.router, prefix="/api", tags=["telemetry"])
app.include_router(security_router.router, prefix="/api", tags=["security"])
app.include_router(worker_router.router, prefix="/api", tags=["workers"])
app.include_router(metrics_router.router, prefix="/api", tags=["metrics"])


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
