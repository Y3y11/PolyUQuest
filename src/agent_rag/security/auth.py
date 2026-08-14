"""FastAPI API-key authentication and hierarchical role dependencies."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from agent_rag.config import settings
from agent_rag.security.credentials import hash_api_key, parse_api_key_records
from agent_rag.security.models import ROLE_RANK, Principal, Role

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class ApiKeyAuthenticator:
    def __init__(self, mode: str, raw_records: str = "") -> None:
        if mode not in {"disabled", "api_key"}:
            raise ValueError("Unsupported API authentication mode")
        self.mode = mode
        self.records = (
            parse_api_key_records(raw_records) if mode == "api_key" else []
        )

    def authenticate(self, secret: str | None) -> Principal:
        if self.mode == "disabled":
            return Principal(
                principal_id="development-bypass",
                role=Role.admin,
                auth_mode="development_bypass",
            )
        if not secret:
            raise ValueError("invalid API credential")
        candidate = hash_api_key(secret)
        matched = None
        for record in self.records:
            if hmac.compare_digest(candidate, record.secret_hash):
                matched = record
        if matched is None:
            raise ValueError("invalid API credential")
        return Principal(
            principal_id=matched.key_id,
            role=matched.role,
            auth_mode="api_key",
        )


security_authenticator = ApiKeyAuthenticator(
    settings.api_auth_mode,
    settings.api_auth_keys,
)


Dependency = Callable[..., Coroutine[Any, Any, Principal]]


def require_role(required: Role) -> Dependency:
    async def dependency(
        request: Request,
        credential: Annotated[str | None, Depends(_api_key_header)],
    ) -> Principal:
        request.state.required_role = required.value
        try:
            principal = security_authenticator.authenticate(credential)
        except ValueError as exc:
            request.state.auth_outcome = "unauthorized"
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API credential",
                headers={"WWW-Authenticate": "ApiKey"},
            ) from exc
        request.state.principal = principal
        if ROLE_RANK[principal.role] < ROLE_RANK[required]:
            request.state.auth_outcome = "forbidden"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role for this operation",
            )
        request.state.auth_outcome = "allowed"
        return principal

    dependency.required_role = required  # type: ignore[attr-defined]
    return dependency


ReaderPrincipal = Annotated[Principal, Depends(require_role(Role.reader))]
OperatorPrincipal = Annotated[Principal, Depends(require_role(Role.operator))]
AdminPrincipal = Annotated[Principal, Depends(require_role(Role.admin))]
