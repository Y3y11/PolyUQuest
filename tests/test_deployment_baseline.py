from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from agent_rag.config import Settings
from agent_rag.security.credentials import hash_api_key
from agent_rag.workers.lifecycle import stop_worker_task

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _production_settings(**overrides):
    values = {
        "_env_file": None,
        "app_environment": "production",
        "api_auth_mode": "api_key",
        "api_auth_keys": f"admin:admin:{hash_api_key('test-admin-secret')}",
        "neo4j_password": "non-default-production-password",
        "llm_provider": "deepseek",
        "deepseek_api_key": "deepseek-test-key",
        "app_runtime_profile": "remote",
        "embedding_provider": "siliconflow",
        "siliconflow_api_key": "siliconflow-test-key",
    }
    values.update(overrides)
    module_state = {
        "sentence_transformers": False,
        "torch": False,
        "FlagEmbedding": False,
    }
    with patch(
        "agent_rag.deployment.runtime_profile._module_available",
        side_effect=lambda module: module_state[module],
    ):
        return Settings(**values)


class ProductionConfigurationTests(unittest.TestCase):
    def test_valid_production_configuration_is_accepted(self) -> None:
        settings = _production_settings()
        self.assertEqual(settings.app_environment, "production")

    def test_default_database_password_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "non-default NEO4J_PASSWORD"):
            _production_settings(neo4j_password="agent_rag_polyu")

    def test_reload_and_missing_provider_key_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "API_RELOAD"):
            _production_settings(api_reload=True)
        with self.assertRaisesRegex(ValidationError, "requires its API key"):
            _production_settings(deepseek_api_key="")

    def test_remote_embedding_requires_credentials(self) -> None:
        with self.assertRaisesRegex(ValidationError, "SILICONFLOW_API_KEY"):
            _production_settings(
                embedding_provider="siliconflow",
                siliconflow_api_key="",
            )

    def test_worker_role_does_not_require_api_credentials(self) -> None:
        settings = _production_settings(
            app_process_role="worker",
            api_auth_mode="disabled",
            api_auth_keys="",
        )
        self.assertEqual(settings.app_process_role, "worker")

    def test_unknown_providers_fail_before_first_request(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unsupported production LLM_PROVIDER"):
            _production_settings(llm_provider="unknown")
        with self.assertRaisesRegex(
            ValidationError, "unsupported production EMBEDDING_PROVIDER"
        ):
            _production_settings(embedding_provider="unknown")

    def test_shutdown_grace_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValidationError, "WORKER_SHUTDOWN_GRACE_SECONDS"):
            Settings(_env_file=None, worker_shutdown_grace_seconds=0)

    def test_production_requires_explicit_runtime_profile(self) -> None:
        with self.assertRaisesRegex(ValidationError, "explicit APP_RUNTIME_PROFILE"):
            _production_settings(app_runtime_profile="auto")


class WorkerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_finishes_inside_grace_period(self) -> None:
        event = asyncio.Event()

        class Worker:
            def stop(self) -> None:
                event.set()

        async def run() -> None:
            await event.wait()

        task = asyncio.create_task(run())
        graceful = await stop_worker_task(
            Worker(), task, grace_seconds=0.2, worker_name="test"
        )
        self.assertTrue(graceful)
        self.assertTrue(task.done())

    async def test_worker_is_cancelled_after_grace_period(self) -> None:
        class Worker:
            def stop(self) -> None:
                pass

        async def run() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(run())
        graceful = await stop_worker_task(
            Worker(), task, grace_seconds=0.01, worker_name="test"
        )
        self.assertFalse(graceful)
        self.assertTrue(task.cancelled())


class DeploymentPolicyTests(unittest.TestCase):
    def test_repository_production_topology_passes_policy(self) -> None:
        validator = _load_script("validate_deployment")
        self.assertEqual(validator.validate_deployment(ROOT), [])

    def test_validator_detects_floating_image(self) -> None:
        validator = _load_script("validate_deployment")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "frontend").mkdir()
            for source, destination in (
                (ROOT / "compose.production.yml", root / "compose.production.yml"),
                (ROOT / "Dockerfile", root / "Dockerfile"),
                (ROOT / "frontend" / "Dockerfile", root / "frontend" / "Dockerfile"),
            ):
                destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            compose = root / "compose.production.yml"
            compose.write_text(
                compose.read_text(encoding="utf-8").replace(
                    "qdrant/qdrant:v1.18.2", "qdrant/qdrant:latest", 1
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                any("floating latest" in error for error in validator.validate_deployment(root))
            )

    def test_backup_command_plan_is_explicit(self) -> None:
        operations = _load_script("deployment_ops")
        env_file = ROOT / "deploy" / ".env.production.example"
        command = operations._compose(env_file, "stop", "api", "worker")
        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertIn(str(ROOT / "compose.production.yml"), command)
        self.assertEqual(command[-3:], ["stop", "api", "worker"])


if __name__ == "__main__":
    unittest.main()
