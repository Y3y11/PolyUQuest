from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_rag.config import Settings
from agent_rag.security.audit import SecurityAuditMiddleware, SecurityAuditRecorder
from agent_rag.security.auth import ApiKeyAuthenticator, require_role
from agent_rag.security.credentials import hash_api_key, parse_api_key_records
from agent_rag.security.models import Role
from agent_rag.security.store import SecurityAuditStore


def _records() -> str:
    return ",".join(
        (
            f"reader-service:reader:{hash_api_key('reader-secret')}",
            f"operator-service:operator:{hash_api_key('operator-secret')}",
            f"admin-service:admin:{hash_api_key('admin-secret')}",
        )
    )


class SecurityConfigurationTests(unittest.TestCase):
    def test_production_cannot_start_with_anonymous_api(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not allowed"):
            Settings(
                _env_file=None,
                app_environment="production",
                api_auth_mode="disabled",
            )

    def test_api_key_config_rejects_malformed_duplicate_and_adminless_records(self) -> None:
        digest = hash_api_key("same")
        invalid_values = (
            "broken",
            f"reader:reader:{digest},reader:admin:{hash_api_key('other')}",
            f"reader:reader:{digest},admin:admin:{digest}",
            f"reader:reader:{digest}",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_api_key_records(value)

    def test_valid_hash_only_config_constructs_production_settings(self) -> None:
        settings = Settings(
            _env_file=None,
            app_environment="production",
            app_runtime_profile="local-ml",
            api_auth_mode="api_key",
            api_auth_keys=_records(),
            end_user_identity_mode="signed_jwt",
            end_user_identity_secret_file="/run/secrets/internal_identity_secret",
            neo4j_password="production-only-password",
            llm_provider="deepseek",
            deepseek_api_key="deepseek-test-key",
            embedding_provider="local",
            agent_run_worker_enabled=False,
        )
        self.assertEqual(settings.api_auth_mode, "api_key")
        self.assertNotIn("admin-secret", settings.api_auth_keys)


class SecurityAuthorizationTests(unittest.TestCase):
    def _app(self, root: Path, authenticator: ApiKeyAuthenticator):
        store = SecurityAuditStore(root / "security.sqlite3")
        recorder = SecurityAuditRecorder(store)
        app = FastAPI()
        app.add_middleware(SecurityAuditMiddleware, recorder=recorder)

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get(
            "/reader/{item_id}",
            dependencies=[Depends(require_role(Role.reader))],
        )
        def reader(item_id: str):
            return {"item_id": item_id}

        @app.get(
            "/operator",
            dependencies=[Depends(require_role(Role.operator))],
        )
        def operator():
            return {"status": "ok"}

        @app.post(
            "/admin",
            dependencies=[Depends(require_role(Role.admin))],
        )
        def admin():
            return {"status": "ok"}

        return app, store, recorder

    def test_missing_wrong_key_and_role_shortfall_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            authenticator = ApiKeyAuthenticator("api_key", _records())
            app, store, _ = self._app(Path(temp_dir), authenticator)
            with patch(
                "agent_rag.security.auth.security_authenticator", authenticator
            ):
                client = TestClient(app)
                missing = client.get("/reader/alpha")
                wrong = client.get(
                    "/reader/alpha", headers={"X-API-Key": "wrong-secret"}
                )
                forbidden = client.get(
                    "/operator", headers={"X-API-Key": "reader-secret"}
                )
                allowed = client.get(
                    "/operator", headers={"X-API-Key": "operator-secret"}
                )
                admin = client.post(
                    "/admin", headers={"X-API-Key": "admin-secret"}
                )

            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.headers["www-authenticate"], "ApiKey")
            self.assertEqual(wrong.status_code, 401)
            self.assertNotIn("wrong-secret", wrong.text)
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(admin.status_code, 200)
            self.assertTrue(allowed.headers["x-request-id"].startswith("req-"))
            self.assertEqual(store.stats()["unauthorized"], 2)
            self.assertEqual(store.stats()["forbidden"], 1)

    def test_audit_uses_route_template_and_never_stores_key_or_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            authenticator = ApiKeyAuthenticator("api_key", _records())
            app, store, _ = self._app(Path(temp_dir), authenticator)
            with patch(
                "agent_rag.security.auth.security_authenticator", authenticator
            ):
                client = TestClient(app)
                response = client.get(
                    "/reader/private-item?token=do-not-store",
                    headers={"X-API-Key": "reader-secret"},
                )
                health = client.get("/health?secret=not-audit-data")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(health.status_code, 200)
            events = store.list(limit=10)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].route, "/reader/{item_id}")
            serialized = events[0].model_dump_json()
            self.assertNotIn("reader-secret", serialized)
            self.assertNotIn("do-not-store", serialized)
            self.assertNotIn("private-item", serialized)

    def test_disabled_development_mode_preserves_local_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            authenticator = ApiKeyAuthenticator("disabled")
            app, store, _ = self._app(Path(temp_dir), authenticator)
            with patch(
                "agent_rag.security.auth.security_authenticator", authenticator
            ):
                response = TestClient(app).post("/admin")
            self.assertEqual(response.status_code, 200)
            event = store.list(limit=1)[0]
            self.assertEqual(event.principal_id, "development-bypass")
            self.assertEqual(event.role, "admin")

    def test_audit_failure_is_fail_open_and_counted(self) -> None:
        class BrokenStore:
            def record(self, event) -> None:
                raise OSError("disk unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            authenticator = ApiKeyAuthenticator("disabled")
            app, _, recorder = self._app(Path(temp_dir), authenticator)
            recorder.store = BrokenStore()
            with patch(
                "agent_rag.security.auth.security_authenticator", authenticator
            ):
                response = TestClient(app).get("/reader/alpha")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(recorder.dropped_writes, 1)


if __name__ == "__main__":
    unittest.main()
