from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from agent_rag.evaluation.governance import validate_dataset_manifest
from agent_rag.evaluation.models import EvaluationCase, ObservedResponse
from agent_rag.evaluation.scoring import score_responses


class DatasetGovernanceTests(unittest.TestCase):
    def _write_dataset(
        self,
        root: Path,
        cases: list[dict],
        *,
        status: str = "draft",
        case_file: str = "cases.jsonl",
        expected_hash: str | None = None,
        minimum_cases: int = 1,
        minimum_gold_ratio: float = 0,
        required_tags: list[str] | None = None,
        reviewer: str = "",
        reviewed_at: str | None = None,
    ) -> Path:
        content = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
        target = root / "cases.jsonl"
        target.write_text(content, encoding="utf-8")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = {
            "dataset_id": "test-dataset",
            "version": "1.0.0",
            "owner": "test-owner",
            "status": status,
            "case_file": case_file,
            "case_file_sha256": expected_hash or actual_hash,
            "created_at": "2026-08-14T00:00:00Z",
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "minimum_cases": minimum_cases,
            "minimum_semantic_gold_ratio": minimum_gold_ratio,
            "required_tags": required_tags or [],
        }
        manifest_path = root / "manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        return manifest_path

    @staticmethod
    def _semantic_case(case_id: str = "gold-1") -> dict:
        return {
            "case_id": case_id,
            "question": "How to apply?",
            "tags": ["admission"],
            "task_type": "procedure",
            "expected_status": "answered",
            "required_facts": [["apply online"]],
            "oracle_type": "semantic_gold",
            "criticality": "blocker",
        }

    def test_draft_dataset_warns_but_does_not_claim_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_dataset(
                Path(temp_dir),
                [self._semantic_case()],
                minimum_cases=10,
                required_tags=["freshness"],
            )
            result = validate_dataset_manifest(path)
            self.assertTrue(result.valid)
            self.assertEqual(len(result.warnings), 2)
            self.assertEqual(result.semantic_gold_cases, 1)

    def test_approved_dataset_fails_closed_on_evidence_and_review_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_dataset(
                Path(temp_dir),
                [self._semantic_case()],
                status="approved",
                minimum_cases=2,
                minimum_gold_ratio=1,
                required_tags=["freshness"],
            )
            result = validate_dataset_manifest(path)
            self.assertFalse(result.valid)
            self.assertTrue(any("reviewer" in error for error in result.errors))
            self.assertTrue(any("case_count" in error for error in result.errors))
            self.assertTrue(any("required tag" in error for error in result.errors))

    def test_hash_secret_and_path_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self._write_dataset(
                root,
                [self._semantic_case()],
                expected_hash="0" * 64,
            )
            result = validate_dataset_manifest(path)
            self.assertFalse(result.valid)
            self.assertIn("case_file_sha256 does not match the case file", result.errors)

            path = self._write_dataset(root, [self._semantic_case()], case_file="../cases.jsonl")
            result = validate_dataset_manifest(path)
            self.assertFalse(result.valid)
            self.assertIn("escapes", result.errors[0])

            secret_case = self._semantic_case("secret")
            secret_case["question"] = "use sk-1234567890abcdef1234"
            path = self._write_dataset(root, [secret_case])
            result = validate_dataset_manifest(path)
            self.assertFalse(result.valid)
            self.assertTrue(any("suspected" in error for error in result.errors))

    def test_invalid_utf8_is_reported_as_validation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = self._write_dataset(root, [self._semantic_case()])
            case_path = root / "cases.jsonl"
            case_path.write_bytes(b"\xff\xfe")
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest["case_file_sha256"] = hashlib.sha256(case_path.read_bytes()).hexdigest()
            manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

            result = validate_dataset_manifest(manifest_path)

            self.assertFalse(result.valid)
            self.assertTrue(any("valid UTF-8" in error for error in result.errors))

    def test_oracle_contracts_and_slice_metrics_are_preserved(self) -> None:
        cases = [
            EvaluationCase.model_validate(self._semantic_case()),
            EvaluationCase(
                case_id="behavior-1",
                question="Refresh?",
                tags=["freshness"],
                task_type="refresh",
                expected_exploration="required",
                oracle_type="behavioral_contract",
            ),
            EvaluationCase(
                case_id="smoke-1",
                question="Ping",
                tags=["smoke"],
                task_type="health",
                oracle_type="smoke",
            ),
        ]
        observations = [
            ObservedResponse(
                case_id="gold-1",
                response_status="answered",
                answer="Apply online.",
                run_id="run-1",
            ),
            ObservedResponse(
                case_id="behavior-1",
                response_status="answered",
                pages_fetched=1,
                run_id="run-2",
            ),
            ObservedResponse(case_id="smoke-1", response_status="answered", run_id="run-3"),
        ]
        report = score_responses(cases, observations, variant="candidate")
        self.assertEqual(report.slices["oracle:semantic_gold"].quality_score, 1)
        self.assertEqual(report.slices["tag:freshness"].operational_score, 1)
        smoke = next(item for item in report.results if item.case_id == "smoke-1")
        self.assertIsNone(smoke.quality_score)
        self.assertIsNone(smoke.operational_score)

    def test_expected_status_is_part_of_behavioral_contract_score(self) -> None:
        case = EvaluationCase(
            case_id="status-contract",
            question="Should the agent abstain?",
            tags=["abstention"],
            task_type="safety",
            expected_status="abstained",
            oracle_type="behavioral_contract",
        )
        report = score_responses(
            [case],
            [
                ObservedResponse(
                    case_id=case.case_id,
                    response_status="answered",
                    run_id="run-status",
                )
            ],
            variant="candidate",
        )

        result = report.results[0]
        self.assertEqual(result.operational_score, 0)
        self.assertIn("unexpected_response_status", result.failures)


if __name__ == "__main__":
    unittest.main()
