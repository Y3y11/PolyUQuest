from __future__ import annotations

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.indexing.worker import IndexWorker
from agent_rag.telemetry.recorder import TelemetryRecorder
from agent_rag.telemetry.store import TelemetryStore
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
                telemetry=TelemetryRecorder(
                    TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
                ),
            )
            result = worker.process_once()
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(outbox.get(job.job_id).status, "succeeded")
            telemetry_store = TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
            telemetry = telemetry_store.list(run_type="indexing")
            self.assertEqual(telemetry[0].root_run_id, patch.run_id)
            self.assertEqual(telemetry[0].response_status, "succeeded")
            detail = telemetry_store.get(telemetry[0].run_id)
            assert detail is not None
            self.assertEqual(
                {span.operation for span in detail.spans},
                {"indexing.queue_wait", "indexing.publish_patch"},
            )

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

    def test_worker_heartbeats_during_long_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            patch = _patch()
            job, _ = outbox.enqueue(patch)
            leases: list[str] = []
            original = outbox.heartbeat

            def heartbeat(*args, **kwargs):
                result = original(*args, **kwargs)
                leases.append(result.lease_until)
                return result

            outbox.heartbeat = heartbeat  # type: ignore[method-assign]

            class Publisher:
                def run(self, _tool_input):
                    time.sleep(1.3)
                    published = patch.model_copy(
                        update={"status": "published", "operation": "update"}
                    )
                    return PublishPatchOutput(
                        patch=published, read_after_write_ok=True
                    )

            worker = IndexWorker(
                outbox,
                publish_factory=Publisher,
                worker_id="worker-heartbeat",
                lease_seconds=3,
            )
            result = worker.process_once()
            self.assertEqual(result.status, "succeeded")
            self.assertGreaterEqual(len(leases), 1)
            self.assertEqual(outbox.get(job.job_id).status, "succeeded")

    def test_worker_does_not_overwrite_job_after_losing_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            patch = _patch()
            job, _ = outbox.enqueue(patch)

            class Publisher:
                def run(self, _tool_input):
                    # Simulate lease expiry and another worker reclaiming the
                    # job while this publisher is still finishing its work.
                    outbox.fail(
                        job.job_id,
                        "worker-stale",
                        "lease reclaimed",
                        retry_base_seconds=0,
                    )
                    outbox.claim("worker-new", lease_seconds=30)
                    raise ConnectionError("stale publisher failed")

            worker = IndexWorker(
                outbox,
                publish_factory=Publisher,
                worker_id="worker-stale",
                retry_base_seconds=0,
            )
            result = worker.process_once()

            self.assertEqual(result.status, "running")
            self.assertEqual(result.worker_id, "worker-new")
            self.assertEqual(outbox.get(job.job_id).worker_id, "worker-new")


if __name__ == "__main__":
    unittest.main()
