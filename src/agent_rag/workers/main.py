"""Standalone index and freshness worker process."""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack

import structlog

from agent_rag.config import settings
from agent_rag.freshness.worker import freshness_worker_lifespan
from agent_rag.indexing.worker import index_worker_lifespan
from agent_rag.workers.bootstrap import bootstrap_background_state

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

    await bootstrap_background_state()
    async with AsyncExitStack() as stack:
        index_worker = await stack.enter_async_context(index_worker_lifespan())
        freshness_worker = await stack.enter_async_context(freshness_worker_lifespan())
        if index_worker is None and freshness_worker is None:
            raise RuntimeError(
                "standalone worker has no enabled loops; enable INDEX_WORKER_ENABLED "
                "or FRESHNESS_WORKER_ENABLED"
            )
        logger.info(
            "worker_process_ready",
            index_worker=index_worker is not None,
            freshness_worker=freshness_worker is not None,
        )
        await event.wait()


def start() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    start()
