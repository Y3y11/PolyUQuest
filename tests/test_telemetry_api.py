from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_rag.api.routes import telemetry_router
from agent_rag.telemetry.models import RunTelemetry
from agent_rag.telemetry.store import TelemetryStore


class TelemetryApiTests(unittest.TestCase):
    def test_list_detail_stats_and_slo_do_not_expose_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
            store.start(
                RunTelemetry(
                    run_id="run-1",
                    run_type="agent_query",
                    root_run_id="run-1",
                    query_hash="abc123",
                    query_length=18,
                )
            )
            store.finish(
                "run-1",
                status="completed",
                response_status="answered",
                completed_at="2026-08-14T00:00:00+00:00",
                duration_ms=120,
            )
            app = FastAPI()
            app.include_router(telemetry_router.router, prefix="/api")
            with patch.object(telemetry_router, "telemetry_store", store):
                client = TestClient(app)
                listed = client.get("/api/telemetry/runs").json()
                detail = client.get("/api/telemetry/runs/run-1").json()
                stats = client.get("/api/telemetry/stats").json()
                slo = client.get("/api/telemetry/slo").json()

            self.assertEqual(listed[0]["run_id"], "run-1")
            self.assertEqual(detail["run"]["response_status"], "answered")
            self.assertEqual(stats["p95_duration_ms"], 120)
            self.assertEqual(slo["status"], "insufficient_data")
            self.assertNotIn("question", str(detail).casefold())


if __name__ == "__main__":
    unittest.main()
