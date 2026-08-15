"""Background consumer for durable indexing jobs."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from opentelemetry.trace import SpanKind

from agent_rag.config import settings
from agent_rag.indexing.outbox import IndexJob, IndexOutbox, index_outbox
from agent_rag.telemetry import TelemetryRecorder, telemetry_recorder
from agent_rag.tools.graph_patch import PublishPatchTool
from agent_rag.tools.schemas import PublishPatchInput
from agent_rag.tracing import trace_runtime

logger = structlog.get_logger(__name__)


class IndexWorker:
    def __init__(
        self,
        outbox: IndexOutbox = index_outbox,
        publish_factory: Callable[[], PublishPatchTool] | None = None,
        *,
        worker_id: str | None = None,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
        retry_base_seconds: float | None = None,
        retry_max_seconds: float | None = None,
        telemetry: TelemetryRecorder = telemetry_recorder,
    ):
        self.outbox = outbox
        if publish_factory is None:
            from agent_rag.runtime import build_publish_patch_tool

            publish_factory = build_publish_patch_tool
        self.publish_factory = publish_factory
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.index_worker_poll_seconds
        )
        self.lease_seconds = lease_seconds or settings.index_worker_lease_seconds
        self.retry_base_seconds = (
            retry_base_seconds
            if retry_base_seconds is not None
            else settings.index_job_retry_base_seconds
        )
        self.retry_max_seconds = (
            retry_max_seconds
            if retry_max_seconds is not None
            else settings.index_job_retry_max_seconds
        )
        self._publisher: PublishPatchTool | None = None
        self._stop = threading.Event()
        self._running = False
        self._last_maintenance = time.monotonic()
        self.telemetry = telemetry

    @property
    def is_running(self) -> bool:
        return self._running and not self._stop.is_set()

    def process_once(self) -> IndexJob | None:
        job = self.outbox.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        with trace_runtime.span(
            "knowledge.index.execute",
            traceparent=job.traceparent,
            kind=SpanKind.CONSUMER,
            attributes={"attempt": job.attempts},
        ):
            return self._process_claimed(job)

    def _process_claimed(self, job: IndexJob) -> IndexJob:
        telemetry_run_id = f"index-{job.job_id}-{job.total_attempts}"
        telemetry_started = time.perf_counter()
        self.telemetry.start_run(
            telemetry_run_id,
            "indexing",
            root_run_id=job.run_id,
            parent_run_id=job.run_id,
            attributes={
                "job_id": job.job_id,
                "patch_id": job.patch_id,
                "attempt": job.attempts,
            },
        )
        try:
            queue_wait_ms = max(
                0,
                int(
                    (
                        datetime.now(UTC) - datetime.fromisoformat(job.created_at)
                    ).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            queue_wait_ms = 0
        self.telemetry.record_span(
            run_id=telemetry_run_id,
            stage="indexing.queue",
            operation="indexing.queue_wait",
            status="succeeded",
            duration_ms=queue_wait_ms,
            attributes={"job_id": job.job_id},
        )
        try:
            if self._publisher is None:
                self._publisher = self.publish_factory()
            heartbeat_stop = threading.Event()
            heartbeat_error: list[Exception] = []

            def _heartbeat() -> None:
                interval = max(1.0, self.lease_seconds / 3)
                while not heartbeat_stop.wait(interval):
                    try:
                        self.outbox.heartbeat(
                            job.job_id,
                            self.worker_id,
                            lease_seconds=self.lease_seconds,
                        )
                    except Exception as exc:
                        heartbeat_error.append(exc)
                        return

            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                name=f"index-heartbeat-{job.job_id[-8:]}",
                daemon=True,
            )
            heartbeat_thread.start()
            publish_started = time.perf_counter()
            try:
                try:
                    with self.telemetry.bind(telemetry_run_id):
                        result = self._publisher.run(
                            PublishPatchInput(patch_id=job.patch_id)
                        )
                except Exception as exc:
                    self.telemetry.record_span(
                        run_id=telemetry_run_id,
                        stage="indexing.publish",
                        operation="indexing.publish_patch",
                        status="failed",
                        duration_ms=int(
                            (time.perf_counter() - publish_started) * 1000
                        ),
                        error_category=exc.__class__.__name__,
                        attributes={"job_id": job.job_id, "patch_id": job.patch_id},
                    )
                    raise
                self.telemetry.record_span(
                    run_id=telemetry_run_id,
                    stage="indexing.publish",
                    operation="indexing.publish_patch",
                    status="succeeded",
                    duration_ms=int((time.perf_counter() - publish_started) * 1000),
                    attributes={
                        "job_id": job.job_id,
                        "patch_id": job.patch_id,
                        "operation": result.patch.operation,
                        "blocks_written": result.blocks_written,
                        "links_written": result.links_written,
                    },
                )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2.0)
            if heartbeat_error:
                raise RuntimeError(
                    f"Index job lease heartbeat failed: {heartbeat_error[-1]}"
                ) from heartbeat_error[-1]
            if not result.read_after_write_ok or result.patch.status != "published":
                raise RuntimeError(f"Patch verification failed: {result.patch.status}")
            completed = self.outbox.succeed(job.job_id, self.worker_id)
            logger.info(
                "index_job_succeeded",
                job_id=job.job_id,
                patch_id=job.patch_id,
                operation=result.patch.operation,
                attempts=completed.attempts,
            )
            self.telemetry.finish_run(
                telemetry_run_id,
                telemetry_started,
                status="completed",
                response_status="succeeded",
                attributes={
                    "job_id": job.job_id,
                    "patch_id": job.patch_id,
                    "operation": result.patch.operation,
                    "blocks_written": result.blocks_written,
                    "links_written": result.links_written,
                    "read_after_write_ok": result.read_after_write_ok,
                },
            )
            return completed
        except Exception as exc:
            try:
                failed = self.outbox.fail(
                    job.job_id,
                    self.worker_id,
                    str(exc),
                    retry_base_seconds=self.retry_base_seconds,
                    retry_max_seconds=self.retry_max_seconds,
                )
            except ValueError:
                # Another worker has reclaimed the expired lease. The stale
                # worker must not overwrite its state or crash the run loop.
                current = self.outbox.get(job.job_id)
                logger.warning(
                    "index_job_lease_lost",
                    job_id=job.job_id,
                    patch_id=job.patch_id,
                    worker_id=self.worker_id,
                    error=str(exc),
                )
                self.telemetry.finish_run(
                    telemetry_run_id,
                    telemetry_started,
                    status="error",
                    response_status="lease_lost",
                    error_category=exc.__class__.__name__,
                )
                return current
            logger.warning(
                "index_job_failed",
                job_id=job.job_id,
                patch_id=job.patch_id,
                status=failed.status,
                attempts=failed.attempts,
                error=str(exc),
            )
            self.telemetry.finish_run(
                telemetry_run_id,
                telemetry_started,
                status="error",
                response_status=failed.status,
                error_category=exc.__class__.__name__,
            )
            return failed

    async def run(self) -> None:
        self._running = True
        logger.info("index_worker_started", worker_id=self.worker_id)
        try:
            while not self._stop.is_set():
                result = await asyncio.to_thread(self.process_once)
                if result is None:
                    now = time.monotonic()
                    if now - self._last_maintenance >= 3600:
                        deleted = await asyncio.to_thread(
                            self.outbox.purge_completed,
                            older_than_days=settings.index_job_retention_days,
                        )
                        if deleted:
                            logger.info("index_jobs_purged", deleted=deleted)
                        self._last_maintenance = now
                    await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info("index_worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stop.set()


@contextlib.asynccontextmanager
async def index_worker_lifespan():
    """Run an in-process worker while preserving a future external-worker path."""
    if not settings.index_worker_enabled:
        yield None
        return
    worker = IndexWorker()
    task = asyncio.create_task(worker.run(), name="index-worker")
    await asyncio.sleep(0)
    try:
        yield worker
    finally:
        from agent_rag.workers.lifecycle import stop_worker_task

        await stop_worker_task(
            worker,
            task,
            grace_seconds=settings.worker_shutdown_grace_seconds,
            worker_name="index",
        )
