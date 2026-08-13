from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse

from agent_rag.api.routes.health_router import liveness, readiness


class HealthEndpointTests(unittest.TestCase):
    def test_liveness_does_not_require_dependencies(self) -> None:
        response = liveness()
        self.assertEqual(response.status, "ok")
        self.assertIsNone(response.neo4j)

    @patch("agent_rag.api.routes.health_router._dependency_status")
    def test_readiness_returns_503_when_embedding_is_unavailable(self, dependencies) -> None:
        dependencies.return_value = (True, True)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    startup_complete=True,
                    embedding_ready=False,
                    bm25_ready=True,
                    index_worker=SimpleNamespace(is_running=True),
                )
            )
        )
        response = readiness(request)
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 503)

    @patch("agent_rag.api.routes.health_router._dependency_status")
    def test_readiness_reports_all_components_ready(self, dependencies) -> None:
        dependencies.return_value = (True, True)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    startup_complete=True,
                    embedding_ready=True,
                    bm25_ready=True,
                    index_worker=SimpleNamespace(is_running=True),
                )
            )
        )
        response = readiness(request)
        self.assertEqual(response.status, "ok")


if __name__ == "__main__":
    unittest.main()
