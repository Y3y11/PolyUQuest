from __future__ import annotations

import asyncio

import pytest

from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse
from agent_rag.runs.store import AgentRunStore
from agent_rag.runs.worker import AgentRunWorker


def _request(query: str = "durable") -> AgentQueryRequest:
    return AgentQueryRequest(
        query=query,
        explore_web=False,
        persist_discoveries=False,
    )


class SuccessfulAgent:
    async def run(self, request, emit, *, run_id, telemetry_run_id):
        assert telemetry_run_id.startswith(f"{run_id}-attempt-")
        await emit("run_started", {"run_id": run_id, "query": request.query})
        await emit(
            "action",
            {"sequence": 1, "action": "polyuquest.search", "status": "succeeded"},
        )
        response = AgentQueryResponse(
            run_id=run_id,
            answer="grounded",
            response_status="answered",
            mode="mode_a",
        )
        await emit("done", response.model_dump(mode="json"))
        return response


@pytest.mark.asyncio
async def test_worker_completes_run_and_persists_events(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-worker-00000001")
    worker = AgentRunWorker(
        store,
        agent_factory=SuccessfulAgent,
        worker_id="agent-worker-a",
        lease_seconds=10,
        poll_seconds=0.01,
    )

    completed = await worker.process_once()
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result is not None and completed.result.answer == "grounded"
    assert [event.event_type for event in store.list_events(run.run_id)] == [
        "run_queued",
        "run_attempt_started",
        "run_started",
        "action",
        "done",
    ]


class RetryAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request, emit, *, run_id, telemetry_run_id):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary")
        return await SuccessfulAgent().run(
            request,
            emit,
            run_id=run_id,
            telemetry_run_id=telemetry_run_id,
        )


@pytest.mark.asyncio
async def test_worker_retries_with_same_durable_run_id(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(
        _request(), "browser-worker-00000002", max_attempts=2
    )
    agent = RetryAgent()
    worker = AgentRunWorker(
        store,
        agent_factory=lambda: agent,
        worker_id="agent-worker-a",
        lease_seconds=10,
        retry_base_seconds=0,
    )

    retry = await worker.process_once()
    assert retry is not None and retry.status == "retry"
    completed = await worker.process_once()
    assert completed is not None and completed.status == "completed"
    assert completed.result is not None and completed.result.run_id == run.run_id
    assert [event.event_type for event in store.list_events(run.run_id)].count(
        "run_attempt_started"
    ) == 2


@pytest.mark.asyncio
async def test_new_worker_reclaims_expired_run_without_new_run_id(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(
        _request("survive worker replacement"),
        "browser-worker-00000004",
        max_attempts=2,
    )
    abandoned = store.claim("agent-worker-old", lease_seconds=1)
    assert abandoned is not None and abandoned.attempts == 1

    await asyncio.sleep(1.05)
    replacement = AgentRunWorker(
        store,
        agent_factory=SuccessfulAgent,
        worker_id="agent-worker-new",
        lease_seconds=10,
    )
    completed = await replacement.process_once()

    assert completed is not None and completed.status == "completed"
    assert completed.run_id == run.run_id
    assert completed.attempts == 2
    attempts = [
        event.attempt
        for event in store.list_events(run.run_id)
        if event.event_type == "run_attempt_started"
    ]
    assert attempts == [1, 2]


class BlockingAgent:
    async def run(self, request, emit, *, run_id, telemetry_run_id):
        await emit("run_started", {"run_id": run_id, "query": request.query})
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_running_cancel_stops_worker_task(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-worker-00000003")
    worker = AgentRunWorker(
        store,
        agent_factory=BlockingAgent,
        worker_id="agent-worker-a",
        lease_seconds=3,
        poll_seconds=0.01,
    )
    task = asyncio.create_task(worker.process_once())
    for _ in range(100):
        current = store.get(run.run_id)
        if current is not None and current.status == "running":
            break
        await asyncio.sleep(0.01)
    store.request_cancel(run.run_id)

    cancelled = await asyncio.wait_for(task, timeout=2)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert store.get(run.run_id).status == "cancelled"
