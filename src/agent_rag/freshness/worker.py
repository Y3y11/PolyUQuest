"""Background conditional revalidation worker for indexed web pages."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

import structlog

from agent_rag.config import agent_config, settings
from agent_rag.freshness.store import (
    PageLifecycleStore,
    PageLifecycleTarget,
    page_lifecycle_store,
)
from agent_rag.indexing.outbox import IndexOutbox, index_outbox
from agent_rag.quality import PageQualityGate, PageQualityStore, page_quality_store
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.tools.fetch import FetchTrustedPageTool
from agent_rag.tools.graph_patch import StagePatchTool
from agent_rag.tools.observations import ObservationStore, observation_store
from agent_rag.tools.schemas import FetchInput, StagePatchInput
from agent_rag.tools.snapshot import PageSnapshotTool

logger = structlog.get_logger(__name__)


class FreshnessWorker:
    def __init__(
        self,
        lifecycle_store: PageLifecycleStore = page_lifecycle_store,
        outbox: IndexOutbox = index_outbox,
        observations: ObservationStore = observation_store,
        *,
        fetch_tool: FetchTrustedPageTool | None = None,
        snapshot_tool: PageSnapshotTool | None = None,
        stage_tool: StagePatchTool | None = None,
        quality_gate: PageQualityGate | None = None,
        quality_store: PageQualityStore = page_quality_store,
        neo4j_factory: Callable[[], Neo4jStore] = Neo4jStore,
        worker_id: str | None = None,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
    ):
        self.lifecycle_store = lifecycle_store
        self.outbox = outbox
        self.observations = observations
        self.fetch_tool = fetch_tool or FetchTrustedPageTool(store=observations)
        self.snapshot_tool = snapshot_tool or PageSnapshotTool(neo4j_factory)
        self.stage_tool = stage_tool or StagePatchTool(observations=observations)
        self.quality_gate = quality_gate or PageQualityGate()
        self.quality_store = quality_store
        self.neo4j_factory = neo4j_factory
        self.worker_id = worker_id or f"freshness-{uuid.uuid4().hex[:12]}"
        self.poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else settings.freshness_worker_poll_seconds
        )
        self.lease_seconds = lease_seconds or settings.freshness_worker_lease_seconds
        self._stop = asyncio.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and not self._stop.is_set()

    async def process_once(self) -> PageLifecycleTarget | None:
        target = await asyncio.to_thread(
            self.lifecycle_store.claim,
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if target is None:
            return None
        try:
            snapshot = await asyncio.to_thread(self.snapshot_tool.run, target.source_url)
            if not snapshot.exists:
                raise RuntimeError("Indexed lifecycle target has no graph snapshot")
            fetch_cfg = agent_config.get("fetch", {})
            fetched = await self.fetch_tool.run(
                FetchInput(
                    url=target.source_url,
                    query=str(snapshot.page.get("title") or target.source_url),
                    run_id=f"refresh-{uuid.uuid4().hex}",
                    timeout_seconds=float(fetch_cfg.get("timeout_seconds", 15)),
                    max_bytes=int(fetch_cfg.get("max_bytes", 5_000_000)),
                    if_none_match=snapshot.page.get("etag") or None,
                    if_modified_since=snapshot.page.get("last_modified") or None,
                )
            )
            if fetched.not_modified or fetched.metadata.content_hash == target.content_hash:
                completed = await asyncio.to_thread(
                    self.lifecycle_store.mark_unchanged,
                    target.source_url,
                    self.worker_id,
                )
                await asyncio.to_thread(self._mirror_lifecycle, completed)
                logger.info(
                    "freshness_target_unchanged",
                    source_url=target.source_url,
                    status_code=fetched.metadata.status_code,
                    next_check_at=completed.next_check_at,
                    ttl_hours=completed.current_ttl_hours,
                )
                return completed

            observation = self.observations.get(fetched.observation_id)
            if observation is None:
                raise RuntimeError("Refresh fetch did not persist its Observation")
            decision = self.quality_gate.evaluate(
                observation, fetched, require_query_relevance=False
            )
            decision = await asyncio.to_thread(self.quality_store.put, decision)
            observation.metadata.update(
                {
                    "quality_action": decision.action,
                    "quality_score": decision.score,
                    "quality_policy_version": decision.policy_version,
                    "quality_decision_id": decision.decision_id,
                }
            )
            await asyncio.to_thread(self.observations.put, observation)
            if decision.action != "index":
                completed = await asyncio.to_thread(
                    self.lifecycle_store.quarantine,
                    target.source_url,
                    self.worker_id,
                    ",".join(decision.reasons),
                )
                await asyncio.to_thread(self._mirror_lifecycle, completed)
                logger.warning(
                    "freshness_target_quarantined",
                    source_url=target.source_url,
                    quality_action=decision.action,
                    reasons=decision.reasons,
                )
                return completed

            existing_job = await asyncio.to_thread(
                self.outbox.get_by_snapshot,
                target.source_url,
                fetched.metadata.content_hash,
            )
            if existing_job is None:
                patch = await asyncio.to_thread(
                    self.stage_tool.run,
                    StagePatchInput(
                        observation_id=fetched.observation_id,
                        run_id=observation.run_id,
                    ),
                )
                job, created = await asyncio.to_thread(self.outbox.enqueue, patch)
                if not created and job.patch_id != patch.patch_id:
                    await asyncio.to_thread(
                        self.stage_tool.discard_duplicate, patch.patch_id
                    )
            else:
                job = existing_job
            if job.status == "dead_letter":
                raise RuntimeError(
                    f"Existing index job is dead-lettered: {job.job_id}"
                )
            if job.status == "succeeded":
                completed = await asyncio.to_thread(
                    self.lifecycle_store.register_indexed,
                    target.source_url,
                    fetched.metadata.content_hash,
                    quality_score=decision.score,
                )
                await asyncio.to_thread(self._mirror_lifecycle, completed)
                return completed
            completed = await asyncio.to_thread(
                self.lifecycle_store.mark_indexing,
                target.source_url,
                self.worker_id,
                job.job_id,
                fetched.metadata.content_hash,
            )
            await asyncio.to_thread(self._mirror_lifecycle, completed)
            logger.info(
                "freshness_target_changed",
                source_url=target.source_url,
                previous_hash=target.content_hash,
                content_hash=fetched.metadata.content_hash,
                job_id=job.job_id,
            )
            return completed
        except Exception as exc:
            try:
                failed = await asyncio.to_thread(
                    self.lifecycle_store.fail,
                    target.source_url,
                    self.worker_id,
                    str(exc),
                )
            except ValueError:
                current = await asyncio.to_thread(
                    self.lifecycle_store.get, target.source_url
                )
                logger.warning(
                    "freshness_target_lease_lost",
                    source_url=target.source_url,
                    error=str(exc),
                )
                return current
            await asyncio.to_thread(self._mirror_lifecycle, failed)
            logger.warning(
                "freshness_target_failed",
                source_url=target.source_url,
                failures=failed.consecutive_failures,
                next_check_at=failed.next_check_at,
                error=str(exc),
            )
            return failed

    async def run(self) -> None:
        self._running = True
        logger.info("freshness_worker_started", worker_id=self.worker_id)
        try:
            while not self._stop.is_set():
                result = await self.process_once()
                if result is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.poll_seconds
                        )
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info("freshness_worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def _mirror_lifecycle(self, target: PageLifecycleTarget) -> None:
        neo4j = None
        try:
            neo4j = self.neo4j_factory()
            neo4j.update_webpage_lifecycle(
                target.source_url, target.model_dump()
            )
        except Exception as exc:
            logger.warning(
                "freshness_graph_mirror_failed",
                source_url=target.source_url,
                status=target.status,
                error=str(exc),
            )
        finally:
            if neo4j is not None:
                neo4j.close()


@contextlib.asynccontextmanager
async def freshness_worker_lifespan():
    if not settings.freshness_worker_enabled:
        yield None
        return
    worker = FreshnessWorker()
    task = asyncio.create_task(worker.run(), name="freshness-worker")
    await asyncio.sleep(0)
    try:
        yield worker
    finally:
        from agent_rag.workers.lifecycle import stop_worker_task

        await stop_worker_task(
            worker,
            task,
            grace_seconds=settings.worker_shutdown_grace_seconds,
            worker_name="freshness",
        )
