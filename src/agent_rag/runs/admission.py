"""Deployment-level admission policy for durable Agent Runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_rag.agent.schemas import AgentQueryRequest
from agent_rag.limits import AGENT_BUDGET_LIMITS

AdmissionCapacityReason = Literal["active_limit", "waiting_limit"]


@dataclass(frozen=True, slots=True)
class AgentRunAdmissionPolicy:
    enabled: bool
    max_active: int
    max_waiting: int
    retry_after_seconds: int
    warn_ratio: float
    max_iterations: int
    max_pages: int
    max_seconds: int

    def __post_init__(self) -> None:
        if self.max_active <= 0:
            raise ValueError("max_active must be positive")
        if self.max_waiting <= 0 or self.max_waiting > self.max_active:
            raise ValueError("max_waiting must be positive and not exceed max_active")
        if self.retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        if not 0 < self.warn_ratio < 1:
            raise ValueError("warn_ratio must be between 0 and 1")
        for field, configured in (
            ("max_iterations", self.max_iterations),
            ("max_pages", self.max_pages),
            ("max_seconds", self.max_seconds),
        ):
            if configured <= 0 or configured > AGENT_BUDGET_LIMITS[field]:
                raise ValueError(
                    f"{field} must be positive and no greater than "
                    f"{AGENT_BUDGET_LIMITS[field]}"
                )

    @classmethod
    def from_settings(cls, selected: Any) -> AgentRunAdmissionPolicy:
        return cls(
            enabled=selected.agent_run_admission_enabled,
            max_active=selected.agent_run_admission_max_active,
            max_waiting=selected.agent_run_admission_max_waiting,
            retry_after_seconds=(
                selected.agent_run_admission_retry_after_seconds
            ),
            warn_ratio=selected.agent_run_admission_warn_ratio,
            max_iterations=selected.agent_run_budget_max_iterations,
            max_pages=selected.agent_run_budget_max_pages,
            max_seconds=selected.agent_run_budget_max_seconds,
        )

    def budget_violation(
        self,
        request: AgentQueryRequest,
    ) -> tuple[str, int, int] | None:
        selected = (
            ("max_iterations", request.budget.max_iterations, self.max_iterations),
            ("max_pages", request.budget.max_pages, self.max_pages),
            ("max_seconds", request.budget.max_seconds, self.max_seconds),
        )
        return next(
            (
                (field, int(requested), int(allowed))
                for field, requested, allowed in selected
                if requested > allowed
            ),
            None,
        )


class AgentRunAdmissionRejectedError(RuntimeError):
    """A transient global capacity limit rejected a new Run."""

    def __init__(
        self,
        *,
        reason: AdmissionCapacityReason,
        retry_after_seconds: int,
        active: int,
        waiting: int,
        max_active: int,
        max_waiting: int,
    ) -> None:
        super().__init__(f"Agent Run admission rejected by {reason}")
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.active = active
        self.waiting = waiting
        self.max_active = max_active
        self.max_waiting = max_waiting

    def detail(self) -> dict[str, Any]:
        return {
            "code": "agent_run_capacity_exceeded",
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "capacity": {
                "active": self.active,
                "max_active": self.max_active,
                "waiting": self.waiting,
                "max_waiting": self.max_waiting,
            },
        }


class AgentRunBudgetRejectedError(ValueError):
    """A non-transient deployment budget ceiling rejected a new Run."""

    def __init__(self, *, field: str, requested: int, allowed: int) -> None:
        super().__init__(f"Agent Run budget {field} exceeds deployment policy")
        self.field = field
        self.requested = requested
        self.allowed = allowed

    def detail(self) -> dict[str, Any]:
        return {
            "code": "agent_run_budget_exceeded",
            "field": self.field,
            "requested": self.requested,
            "allowed": self.allowed,
        }
