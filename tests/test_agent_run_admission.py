from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_rag.agent.schemas import AgentBudget, AgentQueryRequest
from agent_rag.runs.admission import (
    AgentRunAdmissionPolicy,
    AgentRunAdmissionRejectedError,
    AgentRunBudgetRejectedError,
)
from agent_rag.runs.store import AgentRunStore, IdempotencyConflictError


def _policy(
    *,
    max_active: int = 10,
    max_waiting: int = 10,
    max_iterations: int = 8,
    max_pages: int = 20,
    max_seconds: int = 300,
) -> AgentRunAdmissionPolicy:
    return AgentRunAdmissionPolicy(
        enabled=True,
        max_active=max_active,
        max_waiting=max_waiting,
        retry_after_seconds=7,
        warn_ratio=0.8,
        max_iterations=max_iterations,
        max_pages=max_pages,
        max_seconds=max_seconds,
    )


def _request(
    query: str = "How do I apply?",
    *,
    budget: AgentBudget | None = None,
) -> AgentQueryRequest:
    return AgentQueryRequest(
        query=query,
        explore_web=False,
        persist_discoveries=False,
        budget=budget or AgentBudget(),
    )


def test_admission_counts_acceptance_and_idempotent_replay(tmp_path: Path) -> None:
    store = AgentRunStore(tmp_path / "runs.sqlite3", admission_policy=_policy())
    run, created = store.create(_request(), "admission-replay-0001")
    replayed, created_again = store.create(
        _request(),
        "admission-replay-0001",
    )

    assert created is True
    assert created_again is False
    assert replayed.run_id == run.run_id
    stats = store.stats()
    assert stats["admission_accepted_total"] == 1
    assert stats["admission_idempotent_replays_total"] == 1


def test_policy_rejects_internally_inconsistent_limits() -> None:
    with pytest.raises(ValueError, match="max_waiting"):
        _policy(max_active=2, max_waiting=3)


def test_atomic_admission_never_exceeds_active_limit(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    policy = _policy(max_active=3, max_waiting=3)
    store = AgentRunStore(
        path,
        admission_policy=policy,
    )

    def submit(index: int) -> str:
        try:
            run, _ = store.create(
                _request(f"Question {index}"),
                f"admission-concurrent-{index:04d}",
            )
            return run.run_id
        except AgentRunAdmissionRejectedError as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(submit, range(20)))

    accepted = [outcome for outcome in outcomes if outcome.startswith("run-")]
    rejected = [outcome for outcome in outcomes if outcome == "active_limit"]
    assert len(accepted) == 3
    assert len(rejected) == 17
    stats = store.stats()
    assert stats["active"] == 3
    assert stats["admission_accepted_total"] == 3
    assert stats["admission_rejected_active_total"] == 17
    assert AgentRunStore(path, admission_policy=policy).stats()[
        "admission_rejected_active_total"
    ] == 17


def test_waiting_limit_allows_new_run_after_worker_claim(tmp_path: Path) -> None:
    store = AgentRunStore(
        tmp_path / "runs.sqlite3",
        admission_policy=_policy(max_active=10, max_waiting=1),
    )
    store.create(_request(), "admission-waiting-0001")
    with pytest.raises(AgentRunAdmissionRejectedError) as raised:
        store.create(_request("Second"), "admission-waiting-0002")
    assert raised.value.reason == "waiting_limit"

    claimed = store.claim("worker-admission", lease_seconds=30)
    assert claimed is not None
    second, created = store.create(
        _request("Second"),
        "admission-waiting-0002",
    )
    assert created is True
    assert second.status == "queued"
    assert store.stats()["admission_rejected_waiting_total"] == 1


def test_full_capacity_preserves_idempotency_and_conflict_order(tmp_path: Path) -> None:
    store = AgentRunStore(
        tmp_path / "runs.sqlite3",
        admission_policy=_policy(max_active=1, max_waiting=1),
    )
    first, _ = store.create(_request(), "admission-ordering-0001")
    replayed, created = store.create(_request(), "admission-ordering-0001")
    assert created is False
    assert replayed.run_id == first.run_id

    with pytest.raises(IdempotencyConflictError):
        store.create(
            _request("Different payload"),
            "admission-ordering-0001",
        )
    with pytest.raises(AgentRunAdmissionRejectedError) as raised:
        store.create(_request("New payload"), "admission-ordering-0002")
    assert raised.value.reason == "active_limit"


def test_budget_rejection_is_persisted_without_creating_run(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    policy = _policy(max_iterations=2, max_pages=2, max_seconds=60)
    store = AgentRunStore(path, admission_policy=policy)
    request = _request(
        budget=AgentBudget(max_iterations=2, max_pages=3, max_seconds=60)
    )

    with pytest.raises(AgentRunBudgetRejectedError) as raised:
        store.create(request, "admission-budget-0001")
    assert raised.value.detail() == {
        "code": "agent_run_budget_exceeded",
        "field": "max_pages",
        "requested": 3,
        "allowed": 2,
    }

    reopened = AgentRunStore(path, admission_policy=policy)
    stats = reopened.stats()
    assert stats["total"] == 0
    assert stats["admission_rejected_budget_total"] == 1


def test_terminal_cancel_releases_capacity_immediately(tmp_path: Path) -> None:
    store = AgentRunStore(
        tmp_path / "runs.sqlite3",
        admission_policy=_policy(max_active=1, max_waiting=1),
    )
    first, _ = store.create(_request(), "admission-release-0001")
    cancelled = store.request_cancel(first.run_id)
    assert cancelled.status == "cancelled"

    second, created = store.create(
        _request("Replacement"),
        "admission-release-0002",
    )
    assert created is True
    assert second.status == "queued"
    assert store.stats()["active"] == 1


def test_disabled_capacity_still_enforces_deployment_budget(tmp_path: Path) -> None:
    enabled = _policy(max_active=1, max_waiting=1)
    disabled = AgentRunAdmissionPolicy(
        enabled=False,
        max_active=enabled.max_active,
        max_waiting=enabled.max_waiting,
        retry_after_seconds=enabled.retry_after_seconds,
        warn_ratio=enabled.warn_ratio,
        max_iterations=enabled.max_iterations,
        max_pages=2,
        max_seconds=enabled.max_seconds,
    )
    store = AgentRunStore(tmp_path / "runs.sqlite3", admission_policy=disabled)
    for index in range(2):
        store.create(
            _request(f"Question {index}", budget=AgentBudget(max_pages=2)),
            f"admission-disabled-{index:04d}",
        )
    with pytest.raises(AgentRunBudgetRejectedError):
        store.create(
            _request(budget=AgentBudget(max_pages=3)),
            "admission-disabled-budget",
        )
    assert store.stats()["active"] == 2
