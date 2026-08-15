from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

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
    assert events[1].payload["claim_reason"] == "initial"
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
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["active"] == 1
    assert stats["terminal"] == 1
    assert stats["attempts_total"] == 4
    assert stats["retried_runs"] == 2
    assert stats["application_retries"] == 1
    assert stats["lease_reclaims"] == 1
    leased_attempts = [
        event.payload["claim_reason"]
        for event in store.list_events(leased.run_id)
        if event.event_type == "run_attempt_started"
    ]
    assert leased_attempts == ["initial", "lease_reclaim"]


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


def test_legacy_schema_is_migrated_without_reassigning_existing_runs(tmp_path) -> None:
    path = tmp_path / "legacy-runs.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            request_json TEXT NOT NULL,
            traceparent TEXT NOT NULL DEFAULT '',
            result_json TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            lease_until TEXT,
            worker_id TEXT,
            cancel_requested_at TEXT,
            last_error_code TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        );
        INSERT INTO agent_runs(
            run_id, idempotency_key, request_fingerprint, request_json,
            status, max_attempts, available_at, created_at, updated_at
        ) VALUES (
            'run-0123456789abcdef0123456789abcdef',
            'legacy-browser-key-0001', 'fingerprint',
            '{"query":"legacy","explore_web":false,"persist_discoveries":false}',
            'queued', 2, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        stores = list(executor.map(lambda _index: AgentRunStore(path), range(2)))
    store = stores[0]
    migrated = store.get("run-0123456789abcdef0123456789abcdef")
    assert migrated is not None
    assert migrated.tenant_id == "legacy-tenant"
    assert migrated.owner_subject == "legacy-user"

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")}
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(agent_runs)")}
    connection.close()
    assert {"tenant_id", "owner_subject"}.issubset(columns)
    assert "idx_agent_runs_owner" in indexes
