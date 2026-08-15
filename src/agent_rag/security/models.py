"""Stable security identities and audit contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    reader = "reader"
    operator = "operator"
    admin = "admin"


ROLE_RANK = {Role.reader: 1, Role.operator: 2, Role.admin: 3}


class Principal(BaseModel):
    principal_id: str
    role: Role
    auth_mode: str


class EndUserIdentity(BaseModel):
    """Trusted end-user identity propagated separately from workload auth."""

    subject: str
    tenant_id: str
    groups: tuple[str, ...] = ()
    issuer: str
    token_id: str = ""


LEGACY_END_USER = EndUserIdentity(
    subject="legacy-user",
    tenant_id="legacy-tenant",
    groups=(),
    issuer="development-bypass",
)


class SecurityAuditEvent(BaseModel):
    event_id: str
    request_id: str
    created_at: str
    principal_id: str = ""
    role: Literal["", "reader", "operator", "admin"] = ""
    auth_mode: Literal["", "api_key", "development_bypass"] = ""
    method: str = Field(min_length=1, max_length=16)
    route: str = Field(min_length=1, max_length=500)
    required_role: Literal["reader", "operator", "admin"]
    status_code: int = Field(ge=100, le=599)
    outcome: Literal["allowed", "unauthorized", "forbidden", "error"]
