from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agent_rag.agent.schemas import AgentQueryRequest, AgentQueryResponse
from agent_rag.api.routes import agent_router
from agent_rag.runs.store import AgentRunStore


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _request(query: str = "How do I apply?") -> AgentQueryRequest:
    return AgentQueryRequest(
        query=query,
        explore_web=True,
        persist_discoveries=False,
    )


def _response(run_id: str) -> AgentQueryResponse:
    return AgentQueryResponse(
        run_id=run_id,
        answer="Apply through the official admissions portal. [1]",
        response_status="answered",
        mode="mode_b",
    )


@pytest.mark.asyncio
async def test_create_snapshot_and_idempotency_conflict(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    with patch.object(agent_router, "agent_run_store", store):
        created = await agent_router.create_agent_run(
            _request(), "browser-1234567890abcdef"
        )
        replayed = await agent_router.create_agent_run(
            _request(), "browser-1234567890abcdef"
        )
        snapshot = await agent_router.get_agent_run(created.run_id)
        stats = await agent_router.get_agent_run_stats()

        assert created.created is True
        assert replayed.created is False
        assert replayed.run_id == created.run_id
        assert snapshot.status == "queued"
        assert snapshot.last_event_id == 1
        assert stats["queued"] == 1

        with pytest.raises(HTTPException) as raised:
            await agent_router.create_agent_run(
                _request("A different query"), "browser-1234567890abcdef"
            )
        assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_event_replay_honours_cursor_and_terminal_state(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-abcdef1234567890")
    claimed = store.claim("worker-api", lease_seconds=30)
    assert claimed is not None
    store.append_owned_event(
        run.run_id,
        "worker-api",
        claimed.attempts,
        "action",
        {"action": "polyuquest.search", "status": "succeeded"},
    )
    store.complete(
        run.run_id,
        "worker-api",
        claimed.attempts,
        _response(run.run_id),
    )

    with patch.object(agent_router, "agent_run_store", store):
        chunks = [
            chunk
            async for chunk in agent_router._replay_run_events(
                ConnectedRequest(), run.run_id, after=1
            )
        ]
        snapshot = await agent_router.get_agent_run(run.run_id)

    replay = "".join(chunks)
    assert "id: 1\n" not in replay
    assert "id: 2\n" in replay
    assert "event: action" in replay
    assert "event: done" in replay
    assert snapshot.status == "completed"
    assert snapshot.result is not None
    assert snapshot.result.answer.startswith("Apply through")


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_durable_run(tmp_path: Path) -> None:
    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-fedcba0987654321")
    with patch.object(agent_router, "agent_run_store", store):
        chunks = [
            chunk
            async for chunk in agent_router._replay_run_events(
                DisconnectedRequest(), run.run_id, after=0
            )
        ]
    persisted = store.get(run.run_id)
    assert chunks == []
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.cancel_requested_at is None


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_persisted(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    run, _ = store.create(_request(), "browser-cancel123456789")
    with patch.object(agent_router, "agent_run_store", store):
        first = await agent_router.cancel_agent_run(run.run_id)
        second = await agent_router.cancel_agent_run(run.run_id)

    assert first.status == "cancelled"
    assert second.status == "cancelled"
    assert first.cancel_requested is True
