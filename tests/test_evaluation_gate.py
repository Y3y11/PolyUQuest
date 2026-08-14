from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_rag.evaluation.cli import gate as gate_cli
from agent_rag.evaluation.cli import main as evaluation_main
from agent_rag.evaluation.gate import evaluate_release_gate
from agent_rag.evaluation.models import (
    EvaluationCase,
    EvaluationDatasetManifest,
    GateCostRatioLimit,
    GateCriticalPolicy,
    GateEvidencePolicy,
    GateMetricFloor,
    GateRegressionLimit,
    GateSlicePolicy,
    ObservedResponse,
    ReleaseGatePolicy,
)
from agent_rag.evaluation.scoring import score_responses


def _cases() -> list[EvaluationCase]:
    return [
        EvaluationCase(
            case_id="blocker",
            question="How to apply?",
            tags=["admission"],
            task_type="procedure",
            expected_status="answered",
            required_facts=[["apply online"]],
            oracle_type="semantic_gold",
            criticality="blocker",
        ),
        EvaluationCase(
            case_id="critical",
            question="What must not be claimed?",
            tags=["abstention"],
            task_type="safety",
            expected_status="answered",
            forbidden_claims=["guaranteed admission"],
            oracle_type="semantic_gold",
            criticality="critical",
        ),
        EvaluationCase(
            case_id="freshness",
            question="Find the latest policy",
            tags=["freshness"],
            task_type="navigation",
            expected_exploration="required",
            max_elapsed_seconds=10,
            oracle_type="behavioral_contract",
        ),
        EvaluationCase(
            case_id="standard",
            question="Which source?",
            tags=["citation"],
            task_type="fact",
            required_source_patterns=["example.edu/policy"],
            oracle_type="semantic_gold",
        ),
    ]


def _manifest(status: str = "approved") -> EvaluationDatasetManifest:
    return EvaluationDatasetManifest(
        dataset_id="gate-fixture",
        version="1.0.0",
        owner="tests",
        status=status,
        case_file="cases.jsonl",
        case_file_sha256="0" * 64,
        created_at="2026-08-14T00:00:00Z",
        reviewer="reviewer" if status == "approved" else "",
        reviewed_at="2026-08-14T01:00:00Z" if status == "approved" else None,
    )


def _observations(*, blocker_answer: str = "Apply online.") -> list[ObservedResponse]:
    return [
        ObservedResponse(
            case_id="blocker",
            response_status="answered",
            answer=blocker_answer,
            billable_tokens=100,
            elapsed_seconds=2,
            run_id="run-1",
        ),
        ObservedResponse(
            case_id="critical",
            response_status="answered",
            answer="Admission depends on the official review.",
            billable_tokens=80,
            elapsed_seconds=2,
            run_id="run-2",
        ),
        ObservedResponse(
            case_id="freshness",
            response_status="answered",
            pages_fetched=1,
            billable_tokens=120,
            elapsed_seconds=5,
            run_id="run-3",
        ),
        ObservedResponse(
            case_id="standard",
            response_status="answered",
            evidence_urls=["https://example.edu/policy/current"],
            billable_tokens=60,
            elapsed_seconds=1,
            run_id="run-4",
        ),
    ]


def _policy() -> ReleaseGatePolicy:
    return ReleaseGatePolicy(
        policy_id="test-gate",
        version="1.0.0",
        evidence=GateEvidencePolicy(
            minimum_cases=4,
            minimum_semantic_gold_cases=3,
            minimum_semantic_gold_ratio=0.75,
        ),
        candidate_floors=[
            GateMetricFloor(metric="quality_score", minimum=0.8),
            GateMetricFloor(metric="operational_score", minimum=0.8),
            GateMetricFloor(metric="missing_responses", maximum=0),
            GateMetricFloor(metric="forbidden_claim_rate", maximum=0),
        ],
        regression_limits=[
            GateRegressionLimit(
                metric="quality_score",
                direction="higher_better",
                max_degradation=0.05,
            )
        ],
        cost_ratio_limits=[
            GateCostRatioLimit(metric="avg_billable_tokens", maximum_ratio=1.25)
        ],
        critical=GateCriticalPolicy(
            case_metric="overall",
            max_case_degradation=0,
            max_blocker_regressions=0,
            max_critical_regressions=0,
        ),
        slices=GateSlicePolicy(
            minimum_cases=1,
            required_slices=["tag:freshness", "tag:abstention"],
        ),
    )


def _reports(*, blocker_answer: str = "Apply online.", status: str = "approved"):
    cases = _cases()
    baseline = score_responses(
        cases, _observations(), variant="baseline", manifest=_manifest()
    )
    candidate = score_responses(
        cases,
        _observations(blocker_answer=blocker_answer),
        variant="candidate",
        manifest=_manifest(status),
    )
    return baseline, candidate


class EvaluationGateTests(unittest.TestCase):
    def test_compatible_reports_pass_all_release_checks(self) -> None:
        baseline, candidate = _reports()
        decision = evaluate_release_gate(baseline, candidate, _policy())
        self.assertEqual(decision.status, "pass")
        self.assertEqual(decision.failed_checks, 0)
        self.assertTrue(decision.policy_fingerprint)
        self.assertEqual(decision.summary_deltas["quality_score"], 0)
        self.assertTrue(decision.baseline_report_fingerprint)
        self.assertTrue(decision.candidate_report_fingerprint)

    def test_gate_identity_is_deterministic_and_tracks_report_provenance(self) -> None:
        baseline, candidate = _reports()
        first = evaluate_release_gate(baseline, candidate, _policy())
        baseline.created_at = "2099-01-01T00:00:00Z"
        candidate.created_at = "2099-01-01T00:00:01Z"
        second = evaluate_release_gate(baseline, candidate, _policy())
        self.assertEqual(first.gate_id, second.gate_id)
        self.assertEqual(first.decision_fingerprint, second.decision_fingerprint)

        candidate.system_config_fingerprints = ["changed-config"]
        changed = evaluate_release_gate(baseline, candidate, _policy())
        self.assertNotEqual(first.gate_id, changed.gate_id)

    def test_draft_or_underpowered_dataset_is_insufficient_evidence(self) -> None:
        baseline, candidate = _reports(status="draft")
        policy = _policy().model_copy(
            update={
                "evidence": GateEvidencePolicy(
                    minimum_cases=10,
                    minimum_semantic_gold_cases=8,
                    minimum_semantic_gold_ratio=0.9,
                )
            }
        )
        decision = evaluate_release_gate(baseline, candidate, policy)
        self.assertEqual(decision.status, "insufficient_evidence")
        self.assertGreaterEqual(decision.insufficient_checks, 3)

    def test_blocker_quality_regression_fails_gate(self) -> None:
        baseline, candidate = _reports(blocker_answer="No documented process.")
        decision = evaluate_release_gate(baseline, candidate, _policy())
        self.assertEqual(decision.status, "fail")
        blocker = next(
            item for item in decision.checks if item.metric == "blocker_regressions"
        )
        self.assertEqual(blocker.actual, 1)
        self.assertEqual(blocker.outcome, "fail")

    def test_zero_baseline_cost_does_not_hide_new_cost(self) -> None:
        baseline, candidate = _reports()
        baseline.summary["avg_billable_tokens"] = 0
        candidate.summary["avg_billable_tokens"] = 1
        decision = evaluate_release_gate(baseline, candidate, _policy())
        cost = next(
            item
            for item in decision.checks
            if item.category == "cost_ratio" and item.metric == "avg_billable_tokens"
        )
        self.assertEqual(cost.actual, "infinite")
        self.assertEqual(decision.status, "fail")

    def test_missing_required_metric_uses_explicit_policy(self) -> None:
        baseline, candidate = _reports()
        candidate.summary["quality_score"] = None
        decision = evaluate_release_gate(baseline, candidate, _policy())
        self.assertEqual(decision.status, "insufficient_evidence")
        self.assertTrue(
            any(item.outcome == "insufficient" for item in decision.checks)
        )

    def test_incompatible_dataset_snapshot_is_rejected(self) -> None:
        baseline, candidate = _reports()
        candidate.dataset_fingerprint = "different"
        with self.assertRaisesRegex(ValueError, "dataset"):
            evaluate_release_gate(baseline, candidate, _policy())

    def test_gate_cli_returns_stable_pass_fail_and_insufficient_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy_path = root / "policy.yaml"
            policy_path.write_text(
                """
policy_id: test-gate
version: 1.0.0
evidence:
  allowed_dataset_statuses: [approved]
  minimum_cases: 4
  minimum_semantic_gold_cases: 3
  minimum_semantic_gold_ratio: 0.75
candidate_floors:
  - metric: quality_score
    minimum: 0.8
critical:
  max_blocker_regressions: 0
slices:
  minimum_cases: 1
""".lstrip(),
                encoding="utf-8",
            )

            for expected, blocker_answer, status in (
                (0, "Apply online.", "approved"),
                (1, "No documented process.", "approved"),
                (2, "Apply online.", "draft"),
            ):
                baseline, candidate = _reports(
                    blocker_answer=blocker_answer, status=status
                )
                baseline_path = root / f"baseline-{expected}.json"
                candidate_path = root / f"candidate-{expected}.json"
                output_path = root / f"gate-{expected}.json"
                baseline_path.write_text(
                    baseline.model_dump_json(), encoding="utf-8"
                )
                candidate_path.write_text(
                    candidate.model_dump_json(), encoding="utf-8"
                )
                exit_code = gate_cli(
                    argparse.Namespace(
                        baseline=baseline_path,
                        candidate=candidate_path,
                        policy=policy_path,
                        output=output_path,
                    )
                )
                self.assertEqual(exit_code, expected)
                self.assertTrue(output_path.is_file())
                self.assertTrue(output_path.with_suffix(".md").is_file())

    def test_cli_input_error_returns_exit_code_three(self) -> None:
        argv = [
            "agent-rag-evaluation",
            "gate",
            "--baseline",
            "missing-baseline.json",
            "--candidate",
            "missing-candidate.json",
            "--policy",
            "missing-policy.yaml",
            "--output",
            "unused.json",
        ]
        with (
            patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            evaluation_main()
        self.assertEqual(raised.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
