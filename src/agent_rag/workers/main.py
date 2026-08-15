"""Standalone index and freshness worker process."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from contextlib import AsyncExitStack

import structlog

from agent_rag.config import settings
from agent_rag.freshness.worker import freshness_worker_lifespan
from agent_rag.indexing.worker import index_worker_lifespan
from agent_rag.runs.worker import agent_run_worker_lifespan
from agent_rag.workers.bootstrap import bootstrap_background_state
from agent_rag.workers.status import worker_status_store

logger = structlog.get_logger(__name__)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(signal_name: str) -> None:
        logger.info("worker_process_stop_requested", signal=signal_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda _signum, _frame, name=sig.name: request_stop(name))


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    if settings.app_environment == "production" and settings.app_process_role != "worker":
        raise RuntimeError(
            "standalone production worker requires APP_PROCESS_ROLE=worker"
        )
    event = stop_event or asyncio.Event()
    if stop_event is None:
        _install_signal_handlers(event)

    from agent_rag.runtime import prepare_process_runtime

    await asyncio.to_thread(prepare_process_runtime)
    await bootstrap_background_state()
    async with AsyncExitStack() as stack:
        agent_run_worker = await stack.enter_async_context(agent_run_worker_lifespan())
        index_worker = await stack.enter_async_context(index_worker_lifespan())
        freshness_worker = await stack.enter_async_context(freshness_worker_lifespan())
        if (
            agent_run_worker is None
            and index_worker is None
            and freshness_worker is None
        ):
            raise RuntimeError(
                "standalone worker has no enabled loops; enable AGENT_RUN_WORKER_ENABLED, "
                "INDEX_WORKER_ENABLED, or FRESHNESS_WORKER_ENABLED"
            )
        instance_id = f"worker-{uuid.uuid4().hex[:12]}"
        capabilities = [
            name
            for name, enabled in (
                ("agent-run", agent_run_worker is not None),
                ("index", index_worker is not None),
                ("freshness", freshness_worker is not None),
            )
            if enabled
        ]
        status_registered = False
        try:
            worker_status_store.register(
                instance_id,
                pid=os.getpid(),
                capabilities=capabilities,
            )
            status_registered = True
        except Exception as exc:
            logger.warning(
                "worker_status_register_failed",
                instance_id=instance_id,
                error_type=type(exc).__name__,
            )
        logger.info(
            "worker_process_ready",
            instance_id=instance_id,
            agent_run_worker=agent_run_worker is not None,
            index_worker=index_worker is not None,
            freshness_worker=freshness_worker is not None,
        )

        async def heartbeat_loop() -> None:
            while not event.is_set():
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=settings.worker_heartbeat_seconds
                    )
                except TimeoutError:
                    try:
                        await asyncio.to_thread(
                            worker_status_store.heartbeat, instance_id
                        )
                    except Exception as exc:
                        logger.warning(
                            "worker_heartbeat_failed",
                            instance_id=instance_id,
                            error_type=type(exc).__name__,
                        )

        heartbeat_task = (
            asyncio.create_task(heartbeat_loop(), name="worker-heartbeat")
            if status_registered
            else None
        )
        try:
            await event.wait()
        finally:
            event.set()
            if heartbeat_task is not None:
                await heartbeat_task
            if status_registered:
                try:
                    await asyncio.to_thread(worker_status_store.stop, instance_id)
                except Exception as exc:
                    logger.warning(
                        "worker_status_stop_failed",
                        instance_id=instance_id,
                        error_type=type(exc).__name__,
                    )


def start() -> None:
    from agent_rag.tracing import trace_runtime

    try:
        asyncio.run(run_worker())
    finally:
        trace_runtime.shutdown()


if __name__ == "__main__":
    start()
