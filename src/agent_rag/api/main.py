"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_rag.api.routes import agent_router, graph_router, health_router, query_router
from agent_rag.config import settings
from agent_rag.retrieval import _bm25, _embedding

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.startup_complete = False
    app.state.embedding_ready = False
    app.state.bm25_ready = False
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
