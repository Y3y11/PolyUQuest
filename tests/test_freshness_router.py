from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from agent_rag.api.routes.freshness_router import (
    get_freshness_stats,
    get_freshness_target,
    list_freshness_targets,
    pause_freshness_target,
    refresh_target_now,
    resume_freshness_target,
)
from agent_rag.freshness import PageLifecycleStore


class FreshnessRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PageLifecycleStore(
            Path(self.temp_dir.name) / "freshness-api.sqlite3"
        )
        self.url = "https://example.org/docs/guide"
        self.store.register_indexed(self.url, "hash")
        self.patcher = patch(
            "agent_rag.api.routes.freshness_router.page_lifecycle_store", self.store
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_list_detail_stats_and_controls(self):
        self.assertEqual(list_freshness_targets(limit=10)[0].source_url, self.url)
        self.assertEqual(get_freshness_target(self.url).content_hash, "hash")
        self.assertEqual(get_freshness_stats()["total"], 1)
        self.assertEqual(pause_freshness_target(self.url).status, "paused")
        self.assertEqual(resume_freshness_target(self.url).status, "active")
        self.assertEqual(refresh_target_now(self.url).status, "active")

    def test_missing_target_returns_404(self):
        with self.assertRaises(HTTPException) as raised:
            get_freshness_target("https://example.org/missing")
        self.assertEqual(raised.exception.status_code, 404)

    def test_active_lease_returns_conflict_for_pause(self):
        self.store.refresh_now(self.url)
        self.store.claim("worker")
        with self.assertRaises(HTTPException) as raised:
            pause_freshness_target(self.url)
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
