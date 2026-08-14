"""Machine-readable evidence contract for the business E2E gate."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_SECRET = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]+)")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_error(error: BaseException | str) -> str:
    """Keep evidence useful without persisting credentials or multiline logs."""
    return _SECRET.sub("[REDACTED]", str(error)).replace("\n", " ")[:500]


class ContractCheck(BaseModel):
    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    details: dict[str, Any] = Field(default_factory=dict)


class StageEvidence(BaseModel):
    name: str
    status: str = "running"
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    duration_ms: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_category: str = ""
    error: str = ""


class BusinessE2EReport(BaseModel):
    schema_version: int = 1
    scenario_id: str
    scenario_token: str
    code_version: str = ""
    status: str = "running"
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    duration_ms: int = 0
    checks: list[ContractCheck] = Field(default_factory=list)
    stages: list[StageEvidence] = Field(default_factory=list)
    audit_ids: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    error_category: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed" and bool(self.checks) and all(
            item.ok for item in self.checks
        )


class ContractViolationError(RuntimeError):
    """Raised when a failed prerequisite makes later checks meaningless."""


class ContractRecorder:
    def __init__(self, report: BusinessE2EReport):
        self.report = report
        self._started = time.perf_counter()

    def check(
        self,
        name: str,
        condition: bool,
        *,
        expected: Any = None,
        actual: Any = None,
        details: dict[str, Any] | None = None,
        required: bool = False,
    ) -> bool:
        item = ContractCheck(
            name=name,
            ok=bool(condition),
            expected=expected,
            actual=actual,
            details=details or {},
        )
        self.report.checks.append(item)
        if required and not item.ok:
            raise ContractViolationError(
                f"Contract check failed: {name}; expected={expected!r}, actual={actual!r}"
            )
        return item.ok

    @contextmanager
    def stage(self, name: str):
        item = StageEvidence(name=name)
        self.report.stages.append(item)
        started = time.perf_counter()
        try:
            yield item.metrics
        except Exception as exc:
            item.status = "failed"
            item.error_category = type(exc).__name__
            item.error = safe_error(exc)
            raise
        else:
            item.status = "passed"
        finally:
            item.completed_at = utc_now()
            item.duration_ms = int((time.perf_counter() - started) * 1000)

    def finish(self, error: BaseException | None = None) -> BusinessE2EReport:
        self.report.completed_at = utc_now()
        self.report.duration_ms = int((time.perf_counter() - self._started) * 1000)
        if error is not None:
            self.report.status = "failed"
            self.report.error_category = type(error).__name__
            self.report.error = safe_error(error)
        elif self.report.checks and all(item.ok for item in self.report.checks):
            self.report.status = "passed"
        else:
            self.report.status = "failed"
        return self.report

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(self.report.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(destination)
        return destination
