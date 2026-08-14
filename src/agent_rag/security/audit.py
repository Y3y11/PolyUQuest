"""Fail-open request audit middleware with bounded security attributes."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from agent_rag.security.models import ROLE_RANK, Principal, Role, SecurityAuditEvent
from agent_rag.security.store import SecurityAuditStore, security_audit_store

logger = structlog.get_logger(__name__)


class SecurityAuditRecorder:
    def __init__(self, store: SecurityAuditStore) -> None:
        self.store = store
        self._dropped_writes = 0
        self._lock = threading.Lock()

    @property
    def dropped_writes(self) -> int:
        with self._lock:
            return self._dropped_writes

    def record(self, event: SecurityAuditEvent) -> None:
        try:
            self.store.record(event)
        except Exception as exc:
            with self._lock:
                self._dropped_writes += 1
            logger.warning(
                "security_audit_write_failed",
                error_type=type(exc).__name__,
                route=event.route,
                outcome=event.outcome,
            )


security_audit_recorder = SecurityAuditRecorder(security_audit_store)


class SecurityAuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, recorder: SecurityAuditRecorder | None = None) -> None:
        super().__init__(app)
        self.recorder = recorder or security_audit_recorder

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = f"req-{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response: Response | None = None
        raised: Exception | None = None
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            raised = exc
            raise
        finally:
            route_object = request.scope.get("route")
            required_role = _route_required_role(route_object) or getattr(
                request.state, "required_role", ""
            )
            if required_role:
                principal = getattr(request.state, "principal", None)
                outcome = getattr(request.state, "auth_outcome", "error")
                status_code = response.status_code if response is not None else 500
                if raised is not None:
                    outcome = "error"
                route = getattr(route_object, "path", request.url.path)
                event = SecurityAuditEvent(
                    event_id=f"audit-{uuid.uuid4().hex}",
                    request_id=request_id,
                    created_at=datetime.now(UTC).isoformat(),
                    principal_id=(
                        principal.principal_id
                        if isinstance(principal, Principal)
                        else ""
                    ),
                    role=(
                        principal.role.value
                        if isinstance(principal, Principal)
                        else ""
                    ),
                    auth_mode=(
                        principal.auth_mode
                        if isinstance(principal, Principal)
                        else ""
                    ),
                    method=request.method,
                    route=route,
                    required_role=required_role,
                    status_code=status_code,
                    outcome=outcome,
                )
                await asyncio.to_thread(
                    self.recorder.record,
                    event,
                )
            if response is not None:
                response.headers["X-Request-ID"] = request_id


def _route_required_role(route_object) -> str:
    dependant = getattr(route_object, "dependant", None)
    roles = [
        role
        for dependency in getattr(dependant, "dependencies", [])
        if isinstance(
            (role := getattr(dependency.call, "required_role", None)), Role
        )
    ]
    return max(roles, key=ROLE_RANK.__getitem__).value if roles else ""
