from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from agent_rag.agent.schemas import AgentBudget, AgentQueryRequest, AgentQueryResponse
from agent_rag.api.routes import agent_router
from agent_rag.runs.admission import AgentRunAdmissionPolicy
from agent_rag.runs.store import AgentRunStore
from agent_rag.security.models import EndUserIdentity


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


def _identity(subject: str = "alice", tenant_id: str = "tenant-a") -> EndUserIdentity:
    return EndUserIdentity(
        subject=subject,
        tenant_id=tenant_id,
        issuer="test-gateway",
    )


def _admission_policy(
    *, max_active: int = 1, max_waiting: int = 1
) -> AgentRunAdmissionPolicy:
    return AgentRunAdmissionPolicy(
        enabled=True,
        max_active=max_active,
        max_waiting=max_waiting,
        retry_after_seconds=7,
        warn_ratio=0.8,
        max_iterations=5,
        max_pages=10,
        max_seconds=120,
    )


@pytest.mark.asyncio
async def test_create_snapshot_and_idempotency_conflict(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    with patch.object(agent_router, "agent_run_store", store):
        created = await agent_router.create_agent_run(
            _request(), "browser-1234567890abcdef", identity=_identity()
        )
        replayed = await agent_router.create_agent_run(
            _request(), "browser-1234567890abcdef", identity=_identity()
        )
        snapshot = await agent_router.get_agent_run(created.run_id, _identity())
        stats = await agent_router.get_agent_run_stats()

        assert created.created is True
        assert replayed.created is False
        assert replayed.run_id == created.run_id
        assert snapshot.status == "queued"
        assert snapshot.last_event_id == 1
        assert stats["queued"] == 1

        with pytest.raises(HTTPException) as raised:
            await agent_router.create_agent_run(
                _request("A different query"),
                "browser-1234567890abcdef",
                identity=_identity(),
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
                , identity=_identity("legacy-user", "legacy-tenant")
            )
        ]
        snapshot = await agent_router.get_agent_run(
            run.run_id, _identity("legacy-user", "legacy-tenant")
        )

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
                , identity=_identity("legacy-user", "legacy-tenant")
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
        identity = _identity("legacy-user", "legacy-tenant")
        first = await agent_router.cancel_agent_run(run.run_id, identity)
        second = await agent_router.cancel_agent_run(run.run_id, identity)

    assert first.status == "cancelled"
    assert second.status == "cancelled"
    assert first.cancel_requested is True


@pytest.mark.asyncio
async def test_run_ownership_hides_cross_owner_access_and_scopes_idempotency(
    tmp_path: Path,
) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    alice = _identity("alice", "tenant-a")
    bob = _identity("bob", "tenant-a")
    shared_key = "browser-shared-123456789"
    with patch.object(agent_router, "agent_run_store", store):
        alice_run = await agent_router.create_agent_run(
            _request(), shared_key, identity=alice
        )
        bob_run = await agent_router.create_agent_run(
            _request(), shared_key, identity=bob
        )

        assert alice_run.run_id != bob_run.run_id
        assert (await agent_router.get_agent_run(alice_run.run_id, alice)).run_id == (
            alice_run.run_id
        )
        for operation in (
            lambda: agent_router.get_agent_run(alice_run.run_id, bob),
            lambda: agent_router.cancel_agent_run(alice_run.run_id, bob),
        ):
            with pytest.raises(HTTPException) as raised:
                await operation()
            assert raised.value.status_code == 404
            assert raised.value.detail == "Agent Run not found"

    persisted = store.get(alice_run.run_id)
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.owner_subject == "alice"


@pytest.mark.asyncio
async def test_run_health_uses_worker_capability_and_safe_stats(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    store.create(_request(), "browser-health12345678")
    worker = SimpleNamespace(
        instance_id="worker-healthy",
        healthy=True,
        capabilities=["agent-run", "index"],
    )
    status_store = SimpleNamespace(list=lambda **_kwargs: [worker])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_run_worker=None))
    )
    with (
        patch.object(agent_router, "agent_run_store", store),
        patch.object(agent_router, "worker_status_store", status_store),
    ):
        health = await agent_router.get_agent_run_health(request)

    assert isinstance(health, dict)
    assert health["status"] == "ok"
    assert health["worker_source"] == "standalone"
    assert health["stats"]["total"] == 1
    assert "query" not in str(health).casefold()


@pytest.mark.asyncio
async def test_run_health_is_503_without_agent_worker(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    status_store = SimpleNamespace(list=lambda **_kwargs: [])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_run_worker=None))
    )
    with (
        patch.object(agent_router, "agent_run_store", store),
        patch.object(agent_router, "worker_status_store", status_store),
    ):
        health = await agent_router.get_agent_run_health(request)

    assert isinstance(health, JSONResponse)
    assert health.status_code == 503
    assert b"no_healthy_agent_run_worker" in health.body


@pytest.mark.asyncio
async def test_run_health_selects_agent_worker_not_latest_other_capability(
    tmp_path: Path,
) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3")
    index_only = SimpleNamespace(
        instance_id="worker-new-index",
        healthy=True,
        capabilities=["index"],
    )
    agent_worker = SimpleNamespace(
        instance_id="worker-older-agent",
        healthy=True,
        capabilities=["agent-run"],
    )
    status_store = SimpleNamespace(
        list=lambda **_kwargs: [index_only, agent_worker]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_run_worker=None))
    )
    with (
        patch.object(agent_router, "agent_run_store", store),
        patch.object(agent_router, "worker_status_store", status_store),
    ):
        health = await agent_router.get_agent_run_health(request)

    assert isinstance(health, dict)
    assert health["status"] == "ok"
    assert health["worker_instance_id"] == "worker-older-agent"


@pytest.mark.asyncio
async def test_run_creation_returns_safe_429_with_retry_after(tmp_path: Path) -> None:
    store = AgentRunStore(
        tmp_path / "runs.sqlite3",
        admission_policy=_admission_policy(),
    )
    store.create(_request("First"), "browser-capacity-000001")
    with (
        patch.object(agent_router, "agent_run_store", store),
        pytest.raises(HTTPException) as raised,
    ):
        await agent_router.create_agent_run(
            _request("Second"),
            "browser-capacity-000002",
            identity=_identity(),
        )

    error = raised.value
    assert error.status_code == 429
    assert error.headers == {"Retry-After": "7"}
    assert error.detail["code"] == "agent_run_capacity_exceeded"
    assert error.detail["reason"] == "active_limit"
    assert "First" not in str(error.detail)
    assert "Second" not in str(error.detail)


@pytest.mark.asyncio
async def test_run_creation_returns_structured_budget_rejection(tmp_path: Path) -> None:
    store = AgentRunStore(
        tmp_path / "runs.sqlite3",
        admission_policy=_admission_policy(),
    )
    request = AgentQueryRequest(
        query="Large exploration",
        explore_web=True,
        persist_discoveries=False,
        budget=AgentBudget(max_iterations=5, max_pages=11, max_seconds=120),
    )
    with (
        patch.object(agent_router, "agent_run_store", store),
        pytest.raises(HTTPException) as raised,
    ):
        await agent_router.create_agent_run(
            request,
            "browser-budget-0000001",
            identity=_identity(),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == {
        "code": "agent_run_budget_exceeded",
        "field": "max_pages",
        "requested": 11,
        "allowed": 10,
    }


@pytest.mark.asyncio
async def test_run_health_reports_capacity_warning(tmp_path: Path) -> None:
    policy = _admission_policy(max_active=10, max_waiting=10)
    store = AgentRunStore(tmp_path / "runs.sqlite3", admission_policy=policy)
    for index in range(8):
        store.create(_request(f"Question {index}"), f"browser-health-{index:08d}")
    worker = SimpleNamespace(
        instance_id="worker-capacity",
        healthy=True,
        capabilities=["agent-run"],
    )
    status_store = SimpleNamespace(list=lambda **_kwargs: [worker])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_run_worker=None))
    )
    with (
        patch.object(agent_router, "agent_run_store", store),
        patch.object(agent_router, "worker_status_store", status_store),
        patch.object(
            agent_router.AgentRunAdmissionPolicy,
            "from_settings",
            return_value=policy,
        ),
    ):
        health = await agent_router.get_agent_run_health(request)

    assert isinstance(health, dict)
    assert health["status"] == "degraded"
    assert "active_capacity_warning" in health["reasons"]
    assert "waiting_capacity_warning" in health["reasons"]
    assert health["admission"]["active_utilization"] == 0.8
