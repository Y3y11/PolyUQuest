"""Reference-free-safe deterministic scoring and compatibility checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_rag.evaluation.models import (
    CaseScore,
    EvaluationCase,
    EvaluationReport,
    ObservedResponse,
)


def load_cases(path: str | Path) -> list[EvaluationCase]:
    cases = [
        EvaluationCase.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Evaluation dataset contains duplicate case_id values")
    if not cases:
        raise ValueError("Evaluation dataset is empty")
    return cases


def load_observations(path: str | Path) -> list[ObservedResponse]:
    observations = [
        ObservedResponse.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [item.case_id for item in observations]
    if len(ids) != len(set(ids)):
        raise ValueError("Observed responses contain duplicate case_id values")
    return observations


def dataset_fingerprint(cases: list[EvaluationCase]) -> str:
    canonical = json.dumps(
        [case.model_dump(mode="json") for case in cases],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def score_responses(
    cases: list[EvaluationCase],
    observations: list[ObservedResponse],
    *,
    variant: str,
) -> EvaluationReport:
    by_id = {item.case_id: item for item in observations}
    unknown = sorted(set(by_id) - {case.case_id for case in cases})
    if unknown:
        raise ValueError(f"Observations contain unknown cases: {', '.join(unknown)}")
    results: list[CaseScore] = []
    for case in cases:
        observed = by_id.get(case.case_id)
        if observed is None:
            results.append(CaseScore(case_id=case.case_id, failures=["missing_response"]))
            continue
        results.append(_score_case(case, observed))
    metric_names = [
        "status_match",
        "fact_coverage",
        "source_recall",
        "forbidden_claim_rate",
        "exploration_match",
        "persistence_match",
        "latency_budget_match",
        "page_budget_match",
        "quality_score",
        "operational_score",
        "overall",
    ]
    summary: dict[str, float | int | None] = {
        "missing_responses": sum(not item.run_id for item in results)
    }
    for name in metric_names:
        values = [getattr(item, name) for item in results if getattr(item, name) is not None]
        summary[name] = round(sum(values) / len(values), 4) if values else None
    observed_values = list(by_id.values())
    for name in (
        "elapsed_seconds",
        "pages_fetched",
        "fetch_failures",
        "indexing_jobs_queued",
    ):
        values = [float(getattr(item, name)) for item in observed_values]
        summary[f"avg_{name}"] = round(sum(values) / len(values), 4) if values else None
    token_values = [
        item.billable_tokens for item in observed_values if item.billable_tokens is not None
    ]
    summary["avg_billable_tokens"] = (
        round(sum(token_values) / len(token_values), 4) if token_values else None
    )
    config_contract = {
        "evaluator": "deterministic-v1",
        "metrics": metric_names,
        "normalization": "casefold-whitespace-v1",
    }
    return EvaluationReport(
        variant=variant,
        dataset_fingerprint=dataset_fingerprint(cases),
        evaluation_config_fingerprint=hashlib.sha256(
            json.dumps(config_contract, sort_keys=True).encode()
        ).hexdigest(),
        system_config_fingerprints=sorted(
            {
                item.system_config_fingerprint
                for item in observations
                if item.system_config_fingerprint
            }
        ),
        code_versions=sorted({item.code_version for item in observations if item.code_version}),
        cases=len(cases),
        summary=summary,
        results=results,
    )


def compare_reports(baseline: EvaluationReport, candidate: EvaluationReport) -> dict[str, Any]:
    if baseline.dataset_fingerprint != candidate.dataset_fingerprint:
        raise ValueError("Reports use different dataset snapshots")
    if baseline.evaluation_config_fingerprint != candidate.evaluation_config_fingerprint:
        raise ValueError("Reports use incompatible evaluator configurations")
    deltas: dict[str, float | None] = {}
    for key, candidate_value in candidate.summary.items():
        baseline_value = baseline.summary.get(key)
        deltas[key] = (
            round(float(candidate_value) - float(baseline_value), 4)
            if isinstance(candidate_value, (int, float))
            and isinstance(baseline_value, (int, float))
            else None
        )
    return {
        "baseline": baseline.variant,
        "candidate": candidate.variant,
        "dataset_fingerprint": candidate.dataset_fingerprint,
        "deltas": deltas,
    }


def _score_case(case: EvaluationCase, observed: ObservedResponse) -> CaseScore:
    normalized_answer = " ".join(observed.answer.casefold().split())
    metrics: dict[str, float | None] = {}
    failures: list[str] = []
    metrics["status_match"] = (
        float(observed.response_status == case.expected_status)
        if case.expected_status is not None
        else None
    )
    if case.required_facts:
        facts = [
            float(any(term.casefold() in normalized_answer for term in alternatives))
            for alternatives in case.required_facts
        ]
        metrics["fact_coverage"] = sum(facts) / len(facts)
        if metrics["fact_coverage"] < 1:
            failures.append("required_fact_missing")
    else:
        metrics["fact_coverage"] = None
    if case.required_source_patterns:
        matched = [
            float(any(_source_matches(url, pattern) for url in observed.evidence_urls))
            for pattern in case.required_source_patterns
        ]
        metrics["source_recall"] = sum(matched) / len(matched)
        if metrics["source_recall"] < 1:
            failures.append("required_source_missing")
    else:
        metrics["source_recall"] = None
    if case.forbidden_claims:
        violations = sum(claim.casefold() in normalized_answer for claim in case.forbidden_claims)
        metrics["forbidden_claim_rate"] = violations / len(case.forbidden_claims)
        if violations:
            failures.append("forbidden_claim_present")
    else:
        metrics["forbidden_claim_rate"] = None
    metrics["exploration_match"] = _expectation_match(
        case.expected_exploration, observed.pages_fetched > 0
    )
    metrics["persistence_match"] = _expectation_match(
        case.expected_persistence, observed.indexing_jobs_queued > 0
    )
    metrics["latency_budget_match"] = (
        float(observed.elapsed_seconds <= case.max_elapsed_seconds)
        if case.max_elapsed_seconds is not None
        else None
    )
    metrics["page_budget_match"] = (
        float(observed.pages_fetched <= case.max_pages_fetched)
        if case.max_pages_fetched is not None
        else None
    )
    quality_values = [
        1 - value if name == "forbidden_claim_rate" else value
        for name, value in metrics.items()
        if name in {"fact_coverage", "source_recall", "forbidden_claim_rate"} and value is not None
    ]
    operational_values = [
        value
        for name, value in metrics.items()
        if name
        in {
            "exploration_match",
            "persistence_match",
            "latency_budget_match",
            "page_budget_match",
        }
        and value is not None
    ]
    quality_score = sum(quality_values) / len(quality_values) if quality_values else None
    operational_score = (
        sum(operational_values) / len(operational_values) if operational_values else None
    )
    # Never let operational compliance masquerade as answer quality. An
    # aggregate score exists only when the case contains semantic gold.
    overall = (
        sum(value for value in (quality_score, operational_score) if value is not None)
        / sum(value is not None for value in (quality_score, operational_score))
        if quality_score is not None
        else None
    )
    return CaseScore(
        case_id=case.case_id,
        quality_score=(round(quality_score, 4) if quality_score is not None else None),
        operational_score=(round(operational_score, 4) if operational_score is not None else None),
        overall=round(overall, 4) if overall is not None else None,
        failures=failures,
        run_id=observed.run_id,
        **{key: round(value, 4) if value is not None else None for key, value in metrics.items()},
    )


def _expectation_match(expectation: str, happened: bool) -> float | None:
    if expectation == "either":
        return None
    return float(happened == (expectation == "required"))


def _source_matches(url: str, pattern: str) -> bool:
    normalized = urlsplit(url)
    candidate = f"{normalized.netloc.casefold()}{normalized.path.casefold()}"
    return pattern.casefold().strip("*") in candidate
