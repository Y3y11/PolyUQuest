from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_rag.evaluation.models import EvaluationCase, ObservedResponse
from agent_rag.evaluation.scoring import (
    compare_reports,
    load_cases,
    score_responses,
)


class EvaluationTests(unittest.TestCase):
    def test_no_gold_fields_are_reported_as_na(self) -> None:
        case = EvaluationCase(case_id="case-1", question="What changed?")
        observed = ObservedResponse(
            case_id="case-1", response_status="answered", run_id="run-1"
        )
        report = score_responses([case], [observed], variant="baseline")
        result = report.results[0]
        self.assertIsNone(result.fact_coverage)
        self.assertIsNone(result.source_recall)
        self.assertIsNone(result.overall)

    def test_facts_sources_and_operational_expectations_are_scored(self) -> None:
        case = EvaluationCase(
            case_id="case-1",
            question="How to apply?",
            expected_status="answered",
            required_facts=[["online application", "apply online"], ["transcript"]],
            required_source_patterns=["example.edu/admission"],
            forbidden_claims=["guaranteed admission"],
            expected_exploration="required",
            expected_persistence="required",
            max_elapsed_seconds=10,
            max_pages_fetched=2,
        )
        observed = ObservedResponse(
            case_id="case-1",
            response_status="answered",
            answer="Apply online and provide a transcript.",
            evidence_urls=["https://example.edu/admission/phd"],
            pages_fetched=1,
            indexing_jobs_queued=1,
            elapsed_seconds=3,
            run_id="run-1",
        )
        result = score_responses([case], [observed], variant="ours").results[0]
        self.assertEqual(result.overall, 1.0)
        self.assertEqual(result.failures, [])

    def test_duplicate_case_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            line = '{"case_id":"same","question":"q"}\n'
            path.write_text(line + line, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_cases(path)

    def test_compare_rejects_different_dataset_snapshot(self) -> None:
        baseline = score_responses(
            [EvaluationCase(case_id="a", question="A")],
            [ObservedResponse(case_id="a", response_status="answered", run_id="r")],
            variant="baseline",
        )
        candidate = score_responses(
            [EvaluationCase(case_id="b", question="B")],
            [ObservedResponse(case_id="b", response_status="answered", run_id="r")],
            variant="candidate",
        )
        with self.assertRaisesRegex(ValueError, "dataset"):
            compare_reports(baseline, candidate)


if __name__ == "__main__":
    unittest.main()
