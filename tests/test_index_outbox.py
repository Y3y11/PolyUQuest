from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.tools.schemas import GraphPatch


def _patch(patch_id: str = "patch-1", content_hash: str = "hash-1") -> GraphPatch:
    now = datetime.now(UTC).isoformat()
    return GraphPatch(
        patch_id=patch_id,
        observation_id=f"obs-{patch_id}",
        run_id="run-1",
        source_url="https://www.polyu.edu.hk/comp/",
        content_hash=content_hash,
        created_at=now,
        updated_at=now,
    )


class IndexOutboxTests(unittest.TestCase):
    def test_enqueue_is_deduplicated_and_survives_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.sqlite3"
            outbox = IndexOutbox(path)
            first, created_first = outbox.enqueue(_patch(), max_attempts=3)
            second, created_second = outbox.enqueue(
                _patch("patch-duplicate"), max_attempts=3
            )
            restored = IndexOutbox(path).get(first.job_id)
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first.job_id, second.job_id)
            self.assertEqual(restored.status, "pending")

    def test_claim_is_exclusive_and_success_clears_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            queued, _ = outbox.enqueue(_patch())
            claimed = outbox.claim("worker-1", lease_seconds=30)
            competing = outbox.claim("worker-2", lease_seconds=30)
            self.assertEqual(claimed.job_id, queued.job_id)
            self.assertEqual(claimed.attempts, 1)
            self.assertIsNone(competing)
            completed = outbox.succeed(queued.job_id, "worker-1")
            self.assertEqual(completed.status, "succeeded")
            self.assertIsNone(completed.lease_until)

    def test_fail_retries_then_moves_to_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            queued, _ = outbox.enqueue(_patch(), max_attempts=2)
            outbox.claim("worker-1")
            retrying = outbox.fail(
                queued.job_id,
                "worker-1",
                "neo4j unavailable",
                retry_base_seconds=0,
            )
            self.assertEqual(retrying.status, "retry")
            outbox.claim("worker-1")
            terminal = outbox.fail(
                queued.job_id,
                "worker-1",
                "qdrant unavailable",
                retry_base_seconds=0,
            )
            self.assertEqual(terminal.status, "dead_letter")
            retried = outbox.retry(queued.job_id)
            self.assertEqual(retried.status, "pending")
            self.assertEqual(retried.attempts, 0)
            self.assertEqual(retried.total_attempts, 2)
            self.assertEqual(retried.manual_retries, 1)

    def test_expired_running_lease_can_be_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            queued, _ = outbox.enqueue(_patch())
            outbox.claim("worker-crashed", lease_seconds=30)
            expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            with outbox._connect() as connection:  # noqa: SLF001
                connection.execute(
                    "UPDATE index_jobs SET lease_until=? WHERE job_id=?",
                    (expired, queued.job_id),
                )
            reclaimed = outbox.claim("worker-new", lease_seconds=30)
            self.assertEqual(reclaimed.job_id, queued.job_id)
            self.assertEqual(reclaimed.worker_id, "worker-new")
            self.assertEqual(reclaimed.attempts, 2)

    def test_stale_worker_cannot_record_failure_after_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = IndexOutbox(Path(temp_dir) / "ledger.sqlite3")
            queued, _ = outbox.enqueue(_patch())
            outbox.claim("worker-old", lease_seconds=30)
            expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            with outbox._connect() as connection:  # noqa: SLF001
                connection.execute(
                    "UPDATE index_jobs SET lease_until=? WHERE job_id=?",
                    (expired, queued.job_id),
                )
            outbox.claim("worker-new", lease_seconds=30)
            with self.assertRaises(ValueError):
                outbox.fail(queued.job_id, "worker-old", "late failure")
            self.assertEqual(outbox.get(queued.job_id).worker_id, "worker-new")


if __name__ == "__main__":
    unittest.main()
