from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_rag.quality import PageQualityGate, PageQualityStore
from agent_rag.tools.observations import ObservationRecord
from agent_rag.tools.schemas import EvidenceGain, FetchMetadata, FetchOutput


def _fetch(observation_id: str, relevant: int = 2, coverage: float = 0.8) -> FetchOutput:
    return FetchOutput(
        observation_id=observation_id,
        metadata=FetchMetadata(
            requested_url="https://example.org/guide",
            final_url="https://example.org/guide",
            title="Application guide",
            fetched_at="2026-08-13T00:00:00+00:00",
            content_hash="hash",
            status_code=200,
        ),
        evidence_gain=EvidenceGain(
            relevant_blocks=relevant, total_blocks=2, lexical_coverage=coverage
        ),
    )


def _record(observation_id: str, contents: list[str], links: int = 1) -> ObservationRecord:
    return ObservationRecord(
        observation_id=observation_id,
        run_id="run-1",
        raw_html="<html>" + " ".join(contents) + "</html>",
        metadata={
            "url": "https://example.org/guide",
            "title": "Application guide",
            "content_hash": "hash",
        },
        blocks=[
            {"block_id": f"b-{index}", "content": content, "token_count": 100}
            for index, content in enumerate(contents)
        ],
        discovered_links=[{"url": f"https://example.org/{index}"} for index in range(links)],
    )


class PageQualityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = PageQualityGate()

    def test_substantive_generic_page_is_indexed(self) -> None:
        contents = [
            "This guide explains eligibility, required documents, deadlines, review, "
            "and submission steps for applicants. " * 4,
            "Applicants should verify current requirements and provide supporting "
            "records through the official submission portal. " * 4,
        ]
        decision = self.gate.evaluate(_record("obs-index", contents), _fetch("obs-index"))
        self.assertEqual(decision.action, "index")
        self.assertTrue(decision.evidence_usable)
        self.assertGreaterEqual(decision.score, 0.58)

    def test_thin_relevant_page_is_evidence_only(self) -> None:
        content = "The official submission deadline is 30 November for this application cycle. " * 2
        decision = self.gate.evaluate(
            _record("obs-thin", [content]), _fetch("obs-thin", relevant=1)
        )
        self.assertEqual(decision.action, "evidence_only")
        self.assertTrue(decision.evidence_usable)
        self.assertIn("thin_but_usable_content", decision.reasons)

    def test_empty_or_irrelevant_page_is_discarded(self) -> None:
        decision = self.gate.evaluate(
            _record("obs-empty", ["Home"]), _fetch("obs-empty", relevant=0, coverage=0)
        )
        self.assertEqual(decision.action, "discard")
        self.assertFalse(decision.evidence_usable)
        self.assertIn("no_query_relevant_blocks", decision.reasons)


class PageQualityStoreTests(unittest.TestCase):
    def test_decision_is_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quality.sqlite3"
            gate = PageQualityGate()
            record = _record(
                "obs-store",
                ["A reusable official procedure with detailed requirements. " * 10],
            )
            decision = gate.evaluate(record, _fetch("obs-store", relevant=1))
            store = PageQualityStore(path)
            first = store.put(decision)
            duplicate = decision.model_copy(update={"decision_id": "quality-duplicate"})
            second = PageQualityStore(path).put(duplicate)
            self.assertEqual(first.decision_id, second.decision_id)
            self.assertEqual(store.stats()["total"], 1)
            self.assertEqual(store.get(first.decision_id).observation_id, "obs-store")


if __name__ == "__main__":
    unittest.main()
