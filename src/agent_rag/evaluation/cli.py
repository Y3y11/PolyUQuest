"""CLI for executing, scoring, and comparing frozen evaluation snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from agent_rag.evaluation.gate import (
    evaluate_release_gate,
    gate_to_markdown,
    load_gate_policy,
)
from agent_rag.evaluation.governance import (
    load_validated_dataset,
    validate_dataset_manifest,
    validation_to_markdown,
)
from agent_rag.evaluation.models import EvaluationReport, ObservedResponse
from agent_rag.evaluation.reporting import to_markdown
from agent_rag.evaluation.scoring import (
    compare_reports,
    load_cases,
    load_observations,
    score_responses,
)


def _write_report(report: EvaluationReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")


def _load_dataset(args: argparse.Namespace):
    if args.manifest is not None:
        manifest, cases, _ = load_validated_dataset(args.manifest)
        return manifest, cases
    return None, load_cases(args.dataset)


def run(args: argparse.Namespace) -> None:
    manifest, cases = _load_dataset(args)
    observations: list[ObservedResponse] = []
    with httpx.Client(base_url=args.api_url, timeout=args.timeout) as client:
        for case in cases:
            response = client.post(
                "/api/agent/query",
                json={
                    "query": case.question,
                    "explore_web": case.expected_exploration != "forbidden",
                    "persist_discoveries": case.expected_persistence == "required",
                },
            )
            response.raise_for_status()
            payload = response.json()
            run_id = payload.get("run_id", "")
            telemetry = {}
            if run_id:
                telemetry_response = client.get(f"/api/telemetry/runs/{run_id}")
                if telemetry_response.is_success:
                    telemetry = telemetry_response.json().get("run", {})
            observations.append(
                ObservedResponse(
                    case_id=case.case_id,
                    response_status=payload["response_status"],
                    answer=payload.get("answer", ""),
                    evidence_urls=[
                        item.get("source_url", "") for item in payload.get("evidence", [])
                    ],
                    pages_fetched=payload.get("exploration", {}).get("pages_fetched", 0),
                    fetch_failures=payload.get("exploration", {}).get("fetch_failures", 0),
                    indexing_jobs_queued=payload.get("exploration", {}).get(
                        "indexing_jobs_queued", 0
                    ),
                    elapsed_seconds=payload.get("elapsed_seconds", 0),
                    billable_tokens=(
                        telemetry.get("billable_input_tokens", 0)
                        + telemetry.get("billable_output_tokens", 0)
                        if telemetry
                        else None
                    ),
                    system_config_fingerprint=telemetry.get("config_fingerprint", ""),
                    code_version=telemetry.get("code_version", ""),
                    run_id=run_id,
                )
            )
    _write_report(
        score_responses(cases, observations, variant=args.variant, manifest=manifest),
        args.output,
    )


def score(args: argparse.Namespace) -> None:
    manifest, cases = _load_dataset(args)
    report = score_responses(
        cases,
        load_observations(args.responses),
        variant=args.variant,
        manifest=manifest,
    )
    _write_report(report, args.output)


def compare(args: argparse.Namespace) -> None:
    baseline = EvaluationReport.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    candidate = EvaluationReport.model_validate_json(args.candidate.read_text(encoding="utf-8"))
    print(json.dumps(compare_reports(baseline, candidate), ensure_ascii=False, indent=2))


def validate(args: argparse.Namespace) -> int:
    result = validate_dataset_manifest(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(
        validation_to_markdown(result), encoding="utf-8"
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.valid else 3


def gate(args: argparse.Namespace) -> int:
    baseline = EvaluationReport.model_validate_json(
        args.baseline.read_text(encoding="utf-8")
    )
    candidate = EvaluationReport.model_validate_json(
        args.candidate.read_text(encoding="utf-8")
    )
    decision = evaluate_release_gate(
        baseline, candidate, load_gate_policy(args.policy)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(
        gate_to_markdown(decision), encoding="utf-8"
    )
    print(decision.model_dump_json(indent=2))
    return {"pass": 0, "fail": 1, "insufficient_evidence": 2}[decision.status]


def _add_dataset_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dataset", type=Path)
    group.add_argument("--manifest", type=Path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True)
    run_parser = subparsers.add_parser("run")
    _add_dataset_arguments(run_parser)
    run_parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    run_parser.add_argument("--variant", required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--timeout", type=float, default=180.0)
    run_parser.set_defaults(callback=run)
    score_parser = subparsers.add_parser("score")
    _add_dataset_arguments(score_parser)
    score_parser.add_argument("--responses", type=Path, required=True)
    score_parser.add_argument("--variant", required=True)
    score_parser.add_argument("--output", type=Path, required=True)
    score_parser.set_defaults(callback=score)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.set_defaults(callback=compare)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.add_argument("--output", type=Path, required=True)
    validate_parser.set_defaults(callback=validate)
    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--baseline", type=Path, required=True)
    gate_parser.add_argument("--candidate", type=Path, required=True)
    gate_parser.add_argument("--policy", type=Path, required=True)
    gate_parser.add_argument("--output", type=Path, required=True)
    gate_parser.set_defaults(callback=gate)
    args = parser.parse_args()
    try:
        result = args.callback(args)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"evaluation input error: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    raise SystemExit(result if isinstance(result, int) else 0)


if __name__ == "__main__":
    main()
