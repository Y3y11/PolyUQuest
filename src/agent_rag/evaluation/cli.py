"""CLI for executing, scoring, and comparing frozen evaluation snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

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


def run(args: argparse.Namespace) -> None:
    cases = load_cases(args.dataset)
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
    _write_report(score_responses(cases, observations, variant=args.variant), args.output)


def score(args: argparse.Namespace) -> None:
    report = score_responses(
        load_cases(args.dataset), load_observations(args.responses), variant=args.variant
    )
    _write_report(report, args.output)


def compare(args: argparse.Namespace) -> None:
    baseline = EvaluationReport.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    candidate = EvaluationReport.model_validate_json(args.candidate.read_text(encoding="utf-8"))
    print(json.dumps(compare_reports(baseline, candidate), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dataset", type=Path, required=True)
    run_parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    run_parser.add_argument("--variant", required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--timeout", type=float, default=180.0)
    run_parser.set_defaults(callback=run)
    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--dataset", type=Path, required=True)
    score_parser.add_argument("--responses", type=Path, required=True)
    score_parser.add_argument("--variant", required=True)
    score_parser.add_argument("--output", type=Path, required=True)
    score_parser.set_defaults(callback=score)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.set_defaults(callback=compare)
    args = parser.parse_args()
    args.callback(args)


if __name__ == "__main__":
    main()
