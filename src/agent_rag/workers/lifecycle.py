"""Shared lifecycle helpers for background workers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class StoppableWorker(Protocol):
    def stop(self) -> None: ...


async def stop_worker_task(
    worker: StoppableWorker,
    task: asyncio.Task[None],
    *,
    grace_seconds: float,
    worker_name: str,
) -> bool:
    """Stop intake, then wait for current work before cancellation.

    Returns ``True`` when the worker exited inside the grace period. A cancelled
    asyncio task may still have a thread-pool function running; persistence
    operations therefore remain idempotent and recoverable by design.
    """
    worker.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        return True
    except TimeoutError:
        logger.warning(
            "worker_shutdown_grace_exceeded",
            worker=worker_name,
            grace_seconds=grace_seconds,
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return False
