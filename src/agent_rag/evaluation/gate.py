"""Fail-closed release policy evaluation for frozen evaluation reports."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from agent_rag.evaluation.models import (
    EvaluationReport,
    GateCheck,
    GateDecision,
    ReleaseGatePolicy,
)
from agent_rag.evaluation.scoring import compare_reports


def load_gate_policy(path: str | Path) -> ReleaseGatePolicy:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Release gate policy must be a YAML object")
    return ReleaseGatePolicy.model_validate(payload)


def evaluate_release_gate(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    policy: ReleaseGatePolicy,
) -> GateDecision:
    comparison = compare_reports(baseline, candidate)
    if (
        baseline.dataset_id
        and candidate.dataset_id
        and (
            baseline.dataset_id != candidate.dataset_id
            or baseline.dataset_version != candidate.dataset_version
        )
    ):
        raise ValueError("Reports use different dataset manifest identities")
    checks: list[GateCheck] = []
    _check_evidence(candidate, policy, checks)
    for floor in policy.candidate_floors:
        value = candidate.summary.get(floor.metric)
        if not _require_metric(value, floor.metric, "candidate_floor", policy, checks):
            continue
        assert isinstance(value, (int, float))
        passed = (
            value >= floor.minimum
            if floor.minimum is not None
            else value <= float(floor.maximum)
        )
        expected = floor.minimum if floor.minimum is not None else floor.maximum
        comparator = ">=" if floor.minimum is not None else "<="
        checks.append(
            _check(
                "candidate_floor",
                floor.metric,
                passed,
                value,
                expected,
                f"candidate {floor.metric} must be {comparator} {expected}",
            )
        )
    for limit in policy.regression_limits:
        before = baseline.summary.get(limit.metric)
        after = candidate.summary.get(limit.metric)
        if not _require_pair(before, after, limit.metric, "regression", policy, checks):
            continue
        assert isinstance(before, (int, float)) and isinstance(after, (int, float))
        degradation = before - after if limit.direction == "higher_better" else after - before
        checks.append(
            _check(
                "regression",
                limit.metric,
                degradation <= limit.max_degradation,
                round(degradation, 4),
                limit.max_degradation,
                f"{limit.metric} degradation must be <= {limit.max_degradation}",
            )
        )
    for limit in policy.cost_ratio_limits:
        before = baseline.summary.get(limit.metric)
        after = candidate.summary.get(limit.metric)
        if not _require_pair(before, after, limit.metric, "cost_ratio", policy, checks):
            continue
        assert isinstance(before, (int, float)) and isinstance(after, (int, float))
        ratio = _ratio(float(before), float(after))
        checks.append(
            _check(
                "cost_ratio",
                limit.metric,
                ratio <= limit.maximum_ratio,
                ratio if math.isfinite(ratio) else "infinite",
                limit.maximum_ratio,
                f"candidate/baseline {limit.metric} ratio must be <= {limit.maximum_ratio}",
            )
        )
    _check_critical_cases(baseline, candidate, policy, checks)
    _check_slices(baseline, candidate, policy, checks)
    failed = sum(item.outcome == "fail" for item in checks)
    insufficient = sum(item.outcome == "insufficient" for item in checks)
    warnings = sum(item.outcome == "warning" for item in checks)
    status = "fail" if failed else "insufficient_evidence" if insufficient else "pass"
    fingerprint = hashlib.sha256(
        json.dumps(
            policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    baseline_report_fingerprint = _report_fingerprint(baseline)
    candidate_report_fingerprint = _report_fingerprint(candidate)
    decision_material = {
        "policy_fingerprint": fingerprint,
        "dataset_fingerprint": candidate.dataset_fingerprint,
        "baseline_report_fingerprint": baseline_report_fingerprint,
        "candidate_report_fingerprint": candidate_report_fingerprint,
        "status": status,
        "checks": [item.model_dump(mode="json") for item in checks],
    }
    decision_fingerprint = hashlib.sha256(
        json.dumps(decision_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return GateDecision(
        gate_id=f"gate-{decision_fingerprint[:24]}",
        status=status,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_fingerprint=fingerprint,
        decision_fingerprint=decision_fingerprint,
        dataset_id=candidate.dataset_id,
        dataset_version=candidate.dataset_version,
        dataset_status=candidate.dataset_status,
        dataset_fingerprint=candidate.dataset_fingerprint,
        baseline_variant=baseline.variant,
        candidate_variant=candidate.variant,
        baseline_report_fingerprint=baseline_report_fingerprint,
        candidate_report_fingerprint=candidate_report_fingerprint,
        baseline_summary=baseline.summary,
        candidate_summary=candidate.summary,
        summary_deltas=comparison["deltas"],
        failed_checks=failed,
        insufficient_checks=insufficient,
        warning_checks=warnings,
        checks=checks,
    )


def gate_to_markdown(decision: GateDecision) -> str:
    lines = [
        f"# Release gate: {decision.status}",
        "",
        f"- Policy: `{decision.policy_id}@{decision.policy_version}`",
        f"- Dataset: `{decision.dataset_id}@{decision.dataset_version}`",
        f"- Baseline: `{decision.baseline_variant}`",
        f"- Candidate: `{decision.candidate_variant}`",
        f"- Decision fingerprint: `{decision.decision_fingerprint}`",
        f"- Failed: {decision.failed_checks}",
        f"- Insufficient: {decision.insufficient_checks}",
        "",
        "| Outcome | Category | Scope | Metric | Actual | Expected | Message |",
        "|---|---|---|---|---:|---:|---|",
    ]
    ordered = sorted(
        decision.checks,
        key=lambda item: (
            {"fail": 0, "insufficient": 1, "warning": 2, "pass": 3}[item.outcome],
            item.category,
            item.scope,
            item.metric,
        ),
    )
    lines.extend(
        "| "
        + " | ".join(
            str(value).replace("|", "\\|")
            for value in (
                item.outcome,
                item.category,
                item.scope,
                item.metric,
                item.actual if item.actual is not None else "N/A",
                item.expected if item.expected is not None else "N/A",
                item.message,
            )
        )
        + " |"
        for item in ordered
    )
    return "\n".join(lines) + "\n"


def _report_fingerprint(report: EvaluationReport) -> str:
    """Hash decision-relevant report content without wall-clock metadata."""
    payload = report.model_dump(mode="json", exclude={"created_at"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _check_evidence(
    report: EvaluationReport,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
) -> None:
    evidence = policy.evidence
    checks.append(
        _check(
            "evidence",
            "dataset_status",
            report.dataset_status in evidence.allowed_dataset_statuses,
            report.dataset_status or "missing",
            ",".join(evidence.allowed_dataset_statuses),
            "dataset status must be approved by policy",
            insufficient=True,
        )
    )
    checks.append(
        _check(
            "evidence",
            "case_count",
            report.cases >= evidence.minimum_cases,
            report.cases,
            evidence.minimum_cases,
            f"evaluation case count must be >= {evidence.minimum_cases}",
            insufficient=True,
        )
    )
    semantic = report.slices.get("oracle:semantic_gold")
    semantic_count = semantic.case_count if semantic else 0
    ratio = semantic_count / report.cases if report.cases else 0
    checks.append(
        _check(
            "evidence",
            "semantic_gold_cases",
            semantic_count >= evidence.minimum_semantic_gold_cases,
            semantic_count,
            evidence.minimum_semantic_gold_cases,
            "semantic Gold case count must be >= "
            f"{evidence.minimum_semantic_gold_cases}",
            insufficient=True,
        )
    )
    checks.append(
        _check(
            "evidence",
            "semantic_gold_ratio",
            ratio >= evidence.minimum_semantic_gold_ratio,
            round(ratio, 4),
            evidence.minimum_semantic_gold_ratio,
            "semantic Gold ratio must be >= "
            f"{evidence.minimum_semantic_gold_ratio}",
            insufficient=True,
        )
    )


def _check_critical_cases(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
) -> None:
    before = {item.case_id: item for item in baseline.results}
    after = {item.case_id: item for item in candidate.results}
    regressions = {"blocker": 0, "critical": 0}
    metric = policy.critical.case_metric
    for case_id, baseline_case in before.items():
        if baseline_case.criticality not in regressions:
            continue
        candidate_case = after.get(case_id)
        if candidate_case is None:
            regressions[baseline_case.criticality] += 1
            continue
        old = getattr(baseline_case, metric, None)
        new = getattr(candidate_case, metric, None)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            _missing_check(
                f"case:{case_id}", metric, "critical_case", policy, checks
            )
            continue
        if old - new > policy.critical.max_case_degradation:
            regressions[baseline_case.criticality] += 1
    for criticality, maximum in (
        ("blocker", policy.critical.max_blocker_regressions),
        ("critical", policy.critical.max_critical_regressions),
    ):
        checks.append(
            _check(
                "critical_case",
                f"{criticality}_regressions",
                regressions[criticality] <= maximum,
                regressions[criticality],
                maximum,
                f"{criticality} case regressions must be <= {maximum}",
                scope=f"criticality:{criticality}",
            )
        )


def _check_slices(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
) -> None:
    required = set(policy.slices.required_slices)
    for name in sorted(required):
        if name not in baseline.slices or name not in candidate.slices:
            _missing_check(name, "slice", "slice", policy, checks)
    common = set(baseline.slices) & set(candidate.slices)
    selected = {
        name
        for name in common
        if name.startswith(("tag:", "task:"))
        and baseline.slices[name].case_count >= policy.slices.minimum_cases
        and candidate.slices[name].case_count >= policy.slices.minimum_cases
    } | (required & common)
    for name in sorted(selected):
        old_slice = baseline.slices[name]
        new_slice = candidate.slices[name]
        for metric, maximum in (
            ("quality_score", policy.slices.max_quality_degradation),
            ("operational_score", policy.slices.max_operational_degradation),
        ):
            old = getattr(old_slice, metric)
            new = getattr(new_slice, metric)
            if old is None and new is None:
                continue
            if not _require_pair(old, new, metric, "slice", policy, checks, scope=name):
                continue
            assert old is not None and new is not None
            degradation = old - new
            checks.append(
                _check(
                    "slice",
                    metric,
                    degradation <= maximum,
                    round(degradation, 4),
                    maximum,
                    f"slice {metric} degradation must be <= {maximum}",
                    scope=name,
                )
            )


def _ratio(before: float, after: float) -> float:
    if before == 0:
        return 1.0 if after == 0 else math.inf
    return round(after / before, 4)


def _require_metric(
    value: Any,
    metric: str,
    category: str,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
) -> bool:
    if isinstance(value, (int, float)):
        return True
    _missing_check("global", metric, category, policy, checks)
    return False


def _require_pair(
    before: Any,
    after: Any,
    metric: str,
    category: str,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
    *,
    scope: str = "global",
) -> bool:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return True
    _missing_check(scope, metric, category, policy, checks)
    return False


def _missing_check(
    scope: str,
    metric: str,
    category: str,
    policy: ReleaseGatePolicy,
    checks: list[GateCheck],
) -> None:
    outcome = {
        "fail": "fail",
        "insufficient_evidence": "insufficient",
        "ignore": "warning",
    }[policy.missing_metric_policy]
    checks.append(
        GateCheck(
            category=category,
            metric=metric,
            scope=scope,
            outcome=outcome,
            message=f"required metric {metric} is missing",
        )
    )


def _check(
    category: str,
    metric: str,
    passed: bool,
    actual: float | int | str | None,
    expected: float | int | str | None,
    message: str,
    *,
    scope: str = "global",
    insufficient: bool = False,
) -> GateCheck:
    return GateCheck(
        category=category,
        metric=metric,
        scope=scope,
        outcome="pass" if passed else "insufficient" if insufficient else "fail",
        actual=actual,
        expected=expected,
        message=message,
    )
