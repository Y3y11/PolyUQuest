from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.indexing.worker import IndexWorker
from agent_rag.tools.schemas import GraphPatch, PublishPatchOutput


def _patch() -> GraphPatch:
    now = datetime.now(UTC).isoformat()
    return GraphPatch(
        patch_id="patch-worker",
        observation_id="obs-worker",
        run_id="run-worker",
        source_url="https://www.polyu.edu.hk/study/",
        content_hash="worker-hash",
        created_at=now,
        updated_at=now,
    )


class IndexWorkerTests(unittest.TestCase):
    def test_worker_publishes_and_completes_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            patch = _patch()
            job, _ = outbox.enqueue(patch)

            class Publisher:
                def run(self, _tool_input):
                    published = patch.model_copy(
                        update={"status": "published", "operation": "create"}
                    )
                    return PublishPatchOutput(
                        patch=published, read_after_write_ok=True
                    )

            worker = IndexWorker(
                outbox,
                publish_factory=Publisher,
                worker_id="worker-test",
            )
            result = worker.process_once()
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(outbox.get(job.job_id).status, "succeeded")

    def test_worker_failure_is_recorded_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            job, _ = outbox.enqueue(_patch(), max_attempts=3)

            class Publisher:
                def run(self, _tool_input):
                    raise ConnectionError("qdrant unavailable")

            worker = IndexWorker(
                outbox,
                publish_factory=Publisher,
                worker_id="worker-test",
                retry_base_seconds=0,
            )
            result = worker.process_once()
            self.assertEqual(result.status, "retry")
            self.assertIn("qdrant unavailable", outbox.get(job.job_id).last_error)


if __name__ == "__main__":
    unittest.main()
