from __future__ import annotations

import time

import pytest

from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse
from agent_rag.runs.store import (
    AgentRunLeaseLostError,
    AgentRunStore,
    IdempotencyConflictError,
)


def _request(query: str = "How do I apply?") -> AgentQueryRequest:
    return AgentQueryRequest(
        query=query,
        explore_web=False,
        persist_discoveries=False,
    )


def _response(run_id: str) -> AgentQueryResponse:
    return AgentQueryResponse(
        run_id=run_id,
        answer="Apply through the official portal.",
        response_status="answered",
        mode="mode_a",
    )


def test_create_is_idempotent_and_rejects_key_reuse(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    key = "browser-request-00000001"

    first, created = store.create(_request(), key)
    second, created_again = store.create(_request(), key)

    assert created is True
    assert created_again is False
    assert second.run_id == first.run_id
    assert [event.event_type for event in store.list_events(first.run_id)] == [
        "run_queued"
    ]
    with pytest.raises(IdempotencyConflictError):
        store.create(_request("A different question"), key)


def test_claim_events_and_completion_are_replayable(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-request-00000002")
    claimed = store.claim("worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.run_id == run.run_id
    assert claimed.status == "running"
    assert claimed.attempts == 1

    action = store.append_owned_event(
        run.run_id,
        "worker-a",
        1,
        "action",
        {"action": "polyuquest.search", "status": "succeeded"},
    )
    completed = store.complete(
        run.run_id,
        "worker-a",
        1,
        _response(run.run_id),
    )

    assert completed.status == "completed"
    assert completed.result is not None
    assert completed.result.run_id == run.run_id
    events = store.list_events(run.run_id)
    assert [event.event_id for event in events] == sorted(
        event.event_id for event in events
    )
    assert [event.event_type for event in events] == [
        "run_queued",
        "run_attempt_started",
        "action",
        "done",
    ]
    assert [event.event_id for event in store.list_events(run.run_id, after=action.event_id)] == [
        events[-1].event_id
    ]
    assert store.last_event_id(run.run_id) == events[-1].event_id


def test_retry_and_expired_lease_reject_stale_owner(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    retrying, _ = store.create(
        _request("retry"), "browser-request-00000003", max_attempts=2
    )
    first = store.claim("worker-a", lease_seconds=30)
    assert first is not None
    failed = store.fail(
        retrying.run_id,
        "worker-a",
        first.attempts,
        RuntimeError("transient"),
        retry_base_seconds=0,
    )
    assert failed.status == "retry"
    second = store.claim("worker-b", lease_seconds=30)
    assert second is not None
    assert second.attempts == 2
    terminal = store.fail(
        retrying.run_id,
        "worker-b",
        second.attempts,
        RuntimeError("still failing"),
        retry_base_seconds=0,
    )
    assert terminal.status == "failed"
    assert terminal.completed_at is not None

    leased, _ = store.create(
        _request("lease"), "browser-request-00000004", max_attempts=3
    )
    old = store.claim("worker-old", lease_seconds=1)
    assert old is not None
    time.sleep(1.05)
    reclaimed = store.claim("worker-new", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.run_id == leased.run_id
    assert reclaimed.attempts == 2
    with pytest.raises(AgentRunLeaseLostError):
        store.append_owned_event(
            leased.run_id,
            "worker-old",
            1,
            "action",
            {"action": "stale"},
        )


def test_cancel_is_persistent_and_idempotent(tmp_path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    queued, _ = store.create(_request(), "browser-request-00000005")
    cancelled = store.request_cancel(queued.run_id)
    repeated = store.request_cancel(queued.run_id)
    assert cancelled.status == repeated.status == "cancelled"
    assert [event.event_type for event in store.list_events(queued.run_id)] == [
        "run_queued",
        "cancelled",
    ]

    running, _ = store.create(_request(), "browser-request-00000006")
    claimed = store.claim("worker-a", lease_seconds=30)
    assert claimed is not None and claimed.run_id == running.run_id
    requested = store.request_cancel(running.run_id)
    assert requested.status == "running"
    assert requested.cancel_requested_at is not None
    finished = store.finish_cancelled(running.run_id, "worker-a", 1)
    assert finished.status == "cancelled"
    assert [event.event_type for event in store.list_events(running.run_id)][-2:] == [
        "cancel_requested",
        "cancelled",
    ]
