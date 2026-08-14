"""Authenticated identity and admin-only security audit endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from agent_rag.security.audit import security_audit_recorder
from agent_rag.security.auth import AdminPrincipal, ReaderPrincipal
from agent_rag.security.models import Principal, SecurityAuditEvent
from agent_rag.security.store import security_audit_store

router = APIRouter()


@router.get("/security/whoami", response_model=Principal)
def whoami(principal: ReaderPrincipal) -> Principal:
    return principal


@router.get("/security/audit", response_model=list[SecurityAuditEvent])
def list_security_audit(
    _principal: AdminPrincipal,
    principal_id: Annotated[str, Query(max_length=64)] = "",
    outcome: Literal["", "allowed", "unauthorized", "forbidden", "error"] = "",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[SecurityAuditEvent]:
    return security_audit_store.list(
        principal_id=principal_id,
        outcome=outcome,
        limit=limit,
    )


@router.get("/security/audit/stats")
def get_security_audit_stats(_principal: AdminPrincipal) -> dict[str, int]:
    return {
        **security_audit_store.stats(),
        "dropped_writes": security_audit_recorder.dropped_writes,
    }
