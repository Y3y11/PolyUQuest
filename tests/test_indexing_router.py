from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from agent_rag.api.routes.indexing_router import (
    get_index_job,
    get_indexing_stats,
    list_index_jobs,
    retry_index_job,
)
from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.tools.schemas import GraphPatch


class IndexingRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.outbox = IndexOutbox(Path(self.temp_dir.name) / "ledger.sqlite3")
        now = datetime.now(UTC).isoformat()
        patch_model = GraphPatch(
            patch_id="patch-api",
            observation_id="obs-api",
            run_id="run-api",
            source_url="https://www.polyu.edu.hk/comp/",
            content_hash="api-hash",
            created_at=now,
            updated_at=now,
        )
        self.job, _ = self.outbox.enqueue(patch_model, max_attempts=1)
        self.patcher = patch(
            "agent_rag.api.routes.indexing_router.index_outbox", self.outbox
        )
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_list_detail_and_stats(self) -> None:
        self.assertEqual(list_index_jobs(limit=10)[0].job_id, self.job.job_id)
        self.assertEqual(get_index_job(self.job.job_id).status, "pending")
        self.assertEqual(get_indexing_stats()["pending"], 1)

    def test_missing_job_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            get_index_job("missing")
        self.assertEqual(raised.exception.status_code, 404)

    def test_dead_letter_can_be_retried(self) -> None:
        self.outbox.claim("worker-api")
        self.outbox.fail(self.job.job_id, "worker-api", "failed")
        retried = retry_index_job(self.job.job_id)
        self.assertEqual(retried.status, "pending")
        self.assertEqual(retried.manual_retries, 1)


if __name__ == "__main__":
    unittest.main()
