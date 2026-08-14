from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from agent_rag import runtime
from agent_rag.workers import main as worker_main
from agent_rag.workers.status import WorkerStatusStore


def test_worker_status_tracks_healthy_stale_replacement_and_stop(tmp_path: Path) -> None:
    store = WorkerStatusStore(tmp_path / "worker.sqlite3")
    first = store.register(
        "worker-first",
        pid=101,
        capabilities=["freshness", "index"],
    )
    assert first.healthy
    assert first.capabilities == ["freshness", "index"]
    assert store.heartbeat("worker-first").healthy

    time.sleep(0.01)
    stale = store.get("worker-first", max_age_seconds=0.001)
    assert stale is not None
    assert stale.state == "running"
    assert not stale.healthy

    replacement = store.register(
        "worker-second",
        pid=202,
        capabilities=["index"],
    )
    assert replacement.healthy
    latest = store.latest(max_age_seconds=1)
    assert latest is not None
    assert latest.instance_id == "worker-second"
    assert latest.healthy

    stopped = store.stop("worker-second")
    assert stopped.state == "stopped"
    assert not stopped.healthy


def test_worker_status_history_is_bounded_and_newest_first(tmp_path: Path) -> None:
    store = WorkerStatusStore(tmp_path / "history.sqlite3")
    for index in range(3):
        store.register(
            f"worker-{index}",
            pid=100 + index,
            capabilities=["index"],
        )
        time.sleep(0.002)

    records = store.list(limit=2, max_age_seconds=10)
    assert [item.instance_id for item in records] == ["worker-2", "worker-1"]


def test_worker_process_registers_heartbeats_and_stops_cleanly(monkeypatch) -> None:
    calls: list[str] = []

    class FakeStatusStore:
        def register(self, instance_id, *, pid, capabilities):
            assert pid > 0
            assert capabilities == ["index"]
            calls.append(f"register:{instance_id}")

        def heartbeat(self, instance_id):
            calls.append(f"heartbeat:{instance_id}")

        def stop(self, instance_id):
            calls.append(f"stop:{instance_id}")

    @asynccontextmanager
    async def index_lifespan():
        yield object()

    @asynccontextmanager
    async def freshness_lifespan():
        yield None

    @asynccontextmanager
    async def agent_run_lifespan():
        yield None

    async def bootstrap() -> None:
        return None

    monkeypatch.setattr(worker_main, "worker_status_store", FakeStatusStore())
    monkeypatch.setattr(worker_main, "index_worker_lifespan", index_lifespan)
    monkeypatch.setattr(worker_main, "freshness_worker_lifespan", freshness_lifespan)
    monkeypatch.setattr(worker_main, "agent_run_worker_lifespan", agent_run_lifespan)
    monkeypatch.setattr(worker_main, "bootstrap_background_state", bootstrap)
    monkeypatch.setattr(runtime, "prepare_process_runtime", lambda: None)
    monkeypatch.setattr(worker_main.settings, "worker_heartbeat_seconds", 0.005)

    async def exercise() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker_main.run_worker(stop))
        await asyncio.sleep(0.025)
        stop.set()
        await task

    asyncio.run(exercise())

    assert calls[0].startswith("register:worker-")
    assert any(item.startswith("heartbeat:worker-") for item in calls)
    assert calls[-1].startswith("stop:worker-")
