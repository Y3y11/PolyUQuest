"""Recover durable background state before worker loops start."""

from __future__ import annotations

import asyncio

import structlog

from agent_rag.config import settings

logger = structlog.get_logger(__name__)


async def bootstrap_background_state() -> None:
    """Repair interrupted patches and reconcile page lifecycle state."""
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
        from agent_rag.indexing.outbox import index_outbox
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
