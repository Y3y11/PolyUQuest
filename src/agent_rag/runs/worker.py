"""Lease-based standalone worker for durable Agent Runs."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog

from agent_rag.config import settings
from agent_rag.runs.models import AgentRunRecord
from agent_rag.runs.store import (
    AgentRunLeaseLostError,
    AgentRunStore,
    agent_run_store,
)

logger = structlog.get_logger(__name__)


class AgentRunWorker:
    def __init__(
        self,
        store: AgentRunStore = agent_run_store,
        agent_factory: Callable[[], Any] | None = None,
        *,
        worker_id: str | None = None,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
        retry_base_seconds: float | None = None,
    ):
        if agent_factory is None:
            from agent_rag.runtime import build_query_agent

            agent_factory = build_query_agent
        self.store = store
        self.agent_factory = agent_factory
        self.worker_id = worker_id or f"agent-worker-{uuid.uuid4().hex[:12]}"
        self.poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else settings.agent_run_worker_poll_seconds
        )
        self.lease_seconds = lease_seconds or settings.agent_run_lease_seconds
        self.retry_base_seconds = (
            retry_base_seconds
            if retry_base_seconds is not None
            else settings.agent_run_retry_base_seconds
        )
        self._agent: Any | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_maintenance = time.monotonic()

    @property
    def is_running(self) -> bool:
        return self._running and not self._stop.is_set()

    async def process_once(self) -> AgentRunRecord | None:
        run = await asyncio.to_thread(
            self.store.claim,
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if run is None:
            return None
        if self._agent is None:
            self._agent = self.agent_factory()

        pending_done: dict[str, Any] | None = None

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            nonlocal pending_done
            cancel_requested = await asyncio.to_thread(
                self.store.is_cancel_requested,
                run.run_id,
                self.worker_id,
                run.attempts,
            )
            if cancel_requested:
                raise asyncio.CancelledError
            if event_type == "done":
                pending_done = payload
                return
            await asyncio.to_thread(
                self.store.append_owned_event,
                run.run_id,
                self.worker_id,
                run.attempts,
                event_type,
                payload,
            )

        execution = asyncio.create_task(
            self._agent.run(
                run.request,
                emit=emit,
                run_id=run.run_id,
                telemetry_run_id=f"{run.run_id}-attempt-{run.attempts}",
            ),
            name=f"agent-run-{run.run_id[-8:]}",
        )
        heartbeat_interval = max(0.25, self.lease_seconds / 3)
        monitor_interval = min(0.25, heartbeat_interval)
        next_heartbeat = time.monotonic() + heartbeat_interval
        try:
            while not execution.done():
                await asyncio.wait({execution}, timeout=monitor_interval)
                if execution.done():
                    break
                if await asyncio.to_thread(
                    self.store.is_cancel_requested,
                    run.run_id,
                    self.worker_id,
                    run.attempts,
                ):
                    execution.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await execution
                    cancelled = await asyncio.to_thread(
                        self.store.finish_cancelled,
                        run.run_id,
                        self.worker_id,
                        run.attempts,
                    )
                    logger.info(
                        "agent_run_cancelled",
                        run_id=run.run_id,
                        attempt=run.attempts,
                    )
                    return cancelled
                if time.monotonic() >= next_heartbeat:
                    await asyncio.to_thread(
                        self.store.heartbeat,
                        run.run_id,
                        self.worker_id,
                        run.attempts,
                        lease_seconds=self.lease_seconds,
                    )
                    next_heartbeat = time.monotonic() + heartbeat_interval

            result = await execution
            if await asyncio.to_thread(
                self.store.is_cancel_requested,
                run.run_id,
                self.worker_id,
                run.attempts,
            ):
                return await asyncio.to_thread(
                    self.store.finish_cancelled,
                    run.run_id,
                    self.worker_id,
                    run.attempts,
                )
            completed = await asyncio.to_thread(
                self.store.complete,
                run.run_id,
                self.worker_id,
                run.attempts,
                result,
                pending_done,
            )
            logger.info(
                "agent_run_completed",
                run_id=run.run_id,
                attempt=run.attempts,
                response_status=result.response_status,
            )
            return completed
        except AgentRunLeaseLostError:
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            logger.warning(
                "agent_run_lease_lost",
                run_id=run.run_id,
                worker_id=self.worker_id,
                attempt=run.attempts,
            )
            return self.store.get(run.run_id)
        except asyncio.CancelledError:
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            cancel_requested = await asyncio.shield(
                asyncio.to_thread(
                    self.store.is_cancel_requested,
                    run.run_id,
                    self.worker_id,
                    run.attempts,
                )
            )
            if cancel_requested:
                cancelled = await asyncio.shield(
                    asyncio.to_thread(
                        self.store.finish_cancelled,
                        run.run_id,
                        self.worker_id,
                        run.attempts,
                    )
                )
                logger.info(
                    "agent_run_cancelled",
                    run_id=run.run_id,
                    attempt=run.attempts,
                )
                return cancelled
            raise
        except Exception as exc:
            failed = await asyncio.to_thread(
                self.store.fail,
                run.run_id,
                self.worker_id,
                run.attempts,
                exc,
                retry_base_seconds=self.retry_base_seconds,
            )
            logger.warning(
                "agent_run_attempt_failed",
                run_id=run.run_id,
                attempt=run.attempts,
                status=failed.status,
                error_type=type(exc).__name__,
            )
            return failed

    async def run(self) -> None:
        self._running = True
        logger.info("agent_run_worker_started", worker_id=self.worker_id)
        try:
            while not self._stop.is_set():
                result = await self.process_once()
                if result is None:
                    now = time.monotonic()
                    if now - self._last_maintenance >= 3600:
                        deleted = await asyncio.to_thread(
                            self.store.purge_terminal,
                            older_than_days=settings.agent_run_retention_days,
                        )
                        if deleted:
                            logger.info("agent_runs_purged", deleted=deleted)
                        self._last_maintenance = now
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.poll_seconds
                        )
        finally:
            self._running = False
            logger.info("agent_run_worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stop.set()


@contextlib.asynccontextmanager
async def agent_run_worker_lifespan():
    if not settings.agent_run_worker_enabled:
        yield None
        return
    worker = AgentRunWorker()
    task = asyncio.create_task(worker.run(), name="agent-run-worker")
    await asyncio.sleep(0)
    try:
        yield worker
    finally:
        from agent_rag.workers.lifecycle import stop_worker_task

        await stop_worker_task(
            worker,
            task,
            grace_seconds=settings.worker_shutdown_grace_seconds,
            worker_name="agent-run",
        )
