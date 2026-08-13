from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_rag.freshness import FreshnessPolicy, PageLifecycleStore


def _policy() -> FreshnessPolicy:
    return FreshnessPolicy(
        policy_version="test-v1",
        min_ttl_hours=1,
        default_ttl_hours=24,
        max_ttl_hours=240,
        unchanged_multiplier=2,
        changed_multiplier=0.5,
        hot_access_threshold=2,
        hot_access_multiplier=0.5,
        failure_backoff_hours=1,
    )


class FreshnessPolicyTests(unittest.TestCase):
    def test_history_and_access_adapt_ttl(self) -> None:
        policy = _policy()
        self.assertEqual(policy.after_unchanged(24), 48)
        self.assertEqual(policy.after_unchanged(24, access_count=2), 24)
        self.assertEqual(policy.after_changed(24), 12)
        self.assertEqual(policy.after_failure(3), 4)


class PageLifecycleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PageLifecycleStore(
            Path(self.temp_dir.name) / "lifecycle.sqlite3", policy=_policy()
        )
        self.now = datetime(2026, 8, 13, tzinfo=UTC)
        self.url = "https://example.org/docs/guide"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _register(self):
        return self.store.register_indexed(
            self.url, "hash-1", quality_score=0.8, validated_at=self.now
        )

    def test_register_persists_and_reschedules_unchanged(self) -> None:
        created = self._register()
        self.assertEqual(created.status, "active")
        self.store.refresh_now(self.url, now=self.now)
        claimed = self.store.claim("worker-1", now=self.now)
        self.assertEqual(claimed.source_url, self.url)
        completed = self.store.mark_unchanged(
            self.url, "worker-1", validated_at=self.now
        )
        self.assertEqual(completed.unchanged_count, 1)
        self.assertGreater(completed.current_ttl_hours, created.current_ttl_hours)
        recovered = PageLifecycleStore(
            Path(self.temp_dir.name) / "lifecycle.sqlite3", policy=_policy()
        ).get(self.url)
        self.assertEqual(recovered.unchanged_count, 1)

    def test_changed_publication_shortens_ttl(self) -> None:
        first = self._register()
        changed = self.store.register_indexed(
            self.url,
            "hash-2",
            quality_score=0.8,
            validated_at=self.now + timedelta(hours=2),
        )
        self.assertEqual(changed.change_count, 1)
        self.assertLess(changed.current_ttl_hours, first.current_ttl_hours)
        self.assertEqual(changed.content_hash, "hash-2")

    def test_failure_backoff_and_stale_lease_recovery(self) -> None:
        self._register()
        self.store.refresh_now(self.url, now=self.now)
        claimed = self.store.claim("worker-old", lease_seconds=1, now=self.now)
        self.assertEqual(claimed.status, "checking")
        reclaimed = self.store.claim(
            "worker-new", now=self.now + timedelta(seconds=2)
        )
        self.assertEqual(reclaimed.worker_id, "worker-new")
        with self.assertRaises(ValueError):
            self.store.fail(self.url, "worker-old", "late failure")
        failed = self.store.fail(self.url, "worker-new", "timeout")
        self.assertEqual(failed.status, "retry")
        self.assertEqual(failed.consecutive_failures, 1)

    def test_pause_resume_access_and_stats(self) -> None:
        self._register()
        self.assertEqual(self.store.record_access_many([self.url]), 1)
        self.assertEqual(self.store.pause(self.url).status, "paused")
        resumed = self.store.resume(self.url, now=self.now)
        self.assertEqual(resumed.status, "active")
        self.assertEqual(self.store.stats(now=self.now)["due_targets"], 1)

    def test_refresh_now_does_not_break_active_lease(self) -> None:
        self._register()
        self.store.refresh_now(self.url, now=self.now)
        claimed = self.store.claim("worker", now=self.now)
        refreshed = self.store.refresh_now(self.url, now=self.now)
        self.assertEqual(refreshed.status, "checking")
        self.assertEqual(refreshed.worker_id, claimed.worker_id)
        with self.assertRaises(ValueError):
            self.store.pause(self.url)
        with self.assertRaises(ValueError):
            self.store.resume(self.url, now=self.now)

    def test_bootstrap_is_idempotent_and_ignores_stubs(self) -> None:
        pages = [
            {
                "url": self.url,
                "content_hash": "hash-bootstrap",
                "fetched_at": "2026-08-13T00:00:00+00:00",
                "quality_score": 0.8,
            },
            {"url": "https://example.org/stub", "content_hash": ""},
        ]
        self.assertEqual(self.store.bootstrap_indexed_pages(pages), 1)
        self.assertEqual(self.store.bootstrap_indexed_pages(pages), 0)
        self.assertEqual(self.store.stats()["total"], 1)

    def test_recover_stale_indexing_from_terminal_job(self) -> None:
        self._register()
        self.store.refresh_now(self.url, now=self.now)
        self.store.claim("worker", now=self.now)
        self.store.mark_indexing(self.url, "worker", "job-1", "hash-2")
        report = self.store.recover_stale_indexing(
            {"job-1": ("succeeded", None)}
        )
        target = self.store.get(self.url)
        self.assertEqual(report["activated"], 1)
        self.assertEqual(target.status, "active")
        self.assertEqual(target.content_hash, "hash-2")

    def test_recover_missing_index_job_to_retry(self) -> None:
        self._register()
        self.store.refresh_now(self.url, now=self.now)
        self.store.claim("worker", now=self.now)
        self.store.mark_indexing(self.url, "worker", "job-missing", "hash-2")
        report = self.store.recover_stale_indexing({})
        self.assertEqual(report["retried"], 1)
        self.assertEqual(self.store.get(self.url).status, "retry")


if __name__ == "__main__":
    unittest.main()
