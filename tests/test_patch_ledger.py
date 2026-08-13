from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_rag.tools.observations import ObservationRecord, ObservationStore, PatchStore
from agent_rag.tools.patch_recovery import recover_pending_patches
from agent_rag.tools.schemas import GraphPatch, PublishPatchOutput


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PersistentLedgerTests(unittest.TestCase):
    def test_observation_and_patch_survive_store_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.sqlite3"
            observations = ObservationStore(db_path=path)
            patches = PatchStore(db_path=path)
            observations.put(
                ObservationRecord(
                    observation_id="obs-1",
                    run_id="run-1",
                    raw_html="<p>证据</p>",
                    metadata={"url": "https://example.edu/a", "content_hash": "h1"},
                    blocks=[{"block_id": "b1", "content": "证据"}],
                    discovered_links=[{"url": "https://example.edu/b"}],
                )
            )
            patch = GraphPatch(
                patch_id="patch-1",
                observation_id="obs-1",
                run_id="run-1",
                source_url="https://example.edu/a",
                content_hash="h1",
                status="repair_required",
                created_at=_now(),
                updated_at=_now(),
                attempts=1,
            )
            patches.put(patch)

            restored_observation = ObservationStore(db_path=path).get("obs-1")
            restored_patch = PatchStore(db_path=path).get("patch-1")
            self.assertIsNotNone(restored_observation)
            self.assertEqual(restored_observation.raw_html, "<p>证据</p>")
            self.assertEqual(restored_observation.blocks[0]["block_id"], "b1")
            self.assertEqual(restored_patch.status, "repair_required")
            self.assertEqual(restored_patch.attempts, 1)

    def test_recovery_respects_attempt_limit_and_reports_success(self) -> None:
        patches = PatchStore()
        recoverable = GraphPatch(
            patch_id="recoverable",
            observation_id="obs-1",
            run_id="run-1",
            source_url="https://example.edu/a",
            content_hash="h1",
            status="repair_required",
            created_at=_now(),
            updated_at=_now(),
            attempts=1,
        )
        exhausted = recoverable.model_copy(
            update={"patch_id": "exhausted", "attempts": 5}
        )
        patches.put(recoverable)
        patches.put(exhausted)

        class FakePublisher:
            def run(self, tool_input):
                patch = patches.get(tool_input.patch_id)
                patch.status = "published"
                patches.put(patch)
                return PublishPatchOutput(patch=patch, read_after_write_ok=True)

        report = recover_pending_patches(
            patches,
            publish_factory=FakePublisher,
            limit=10,
            max_attempts=5,
        )
        self.assertEqual(report.scanned, 2)
        self.assertEqual(report.recovered, 1)
        self.assertEqual(report.skipped, 1)


if __name__ == "__main__":
    unittest.main()
