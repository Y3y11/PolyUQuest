from __future__ import annotations

import unittest

from agent_rag.tools.graph_patch import StagePatchTool
from agent_rag.tools.observations import ObservationRecord, ObservationStore, PatchStore
from agent_rag.tools.schemas import StagePatchInput


class StagePatchTests(unittest.TestCase):
    def test_patch_keeps_run_and_source_provenance(self) -> None:
        observations = ObservationStore()
        observations.put(
            ObservationRecord(
                observation_id="obs-1",
                run_id="run-1",
                raw_html="<p>evidence</p>",
                metadata={
                    "url": "https://www.polyu.edu.hk/study/",
                    "content_hash": "abc",
                },
                blocks=[{"block_id": "b1", "content": "evidence"}],
            )
        )
        tool = StagePatchTool(observations=observations, patches=PatchStore())
        patch = tool.run(StagePatchInput(observation_id="obs-1", run_id="run-1"))
        self.assertEqual(patch.status, "staged")
        self.assertEqual(patch.source_url, "https://www.polyu.edu.hk/study/")
        self.assertEqual(patch.content_hash, "abc")

    def test_patch_rejects_cross_run_observation(self) -> None:
        observations = ObservationStore()
        observations.put(
            ObservationRecord(
                observation_id="obs-1",
                run_id="run-1",
                raw_html="<p>evidence</p>",
                metadata={"url": "https://www.polyu.edu.hk/", "content_hash": "abc"},
                blocks=[{"block_id": "b1", "content": "evidence"}],
            )
        )
        tool = StagePatchTool(observations=observations, patches=PatchStore())
        with self.assertRaises(ValueError):
            tool.run(StagePatchInput(observation_id="obs-1", run_id="run-2"))


if __name__ == "__main__":
    unittest.main()
