"""Dataset manifest loading and fail-closed governance validation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

import yaml

from agent_rag.evaluation.models import (
    DatasetValidationResult,
    EvaluationCase,
    EvaluationDatasetManifest,
)
from agent_rag.evaluation.scoring import dataset_fingerprint, load_cases

_SECRET_PATTERN = re.compile(
    r"(?i)(sk-[a-z0-9_-]{16,}|bearer\s+[a-z0-9._-]{16,}|api[_-]?key\s*[:=])"
)


def load_manifest(path: str | Path) -> EvaluationDatasetManifest:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Evaluation manifest must be a YAML object")
    return EvaluationDatasetManifest.model_validate(payload)


def resolve_case_path(
    manifest_path: str | Path, manifest: EvaluationDatasetManifest
) -> Path:
    parent = Path(manifest_path).resolve().parent
    case_path = (parent / manifest.case_file).resolve()
    if case_path != parent and parent not in case_path.parents:
        raise ValueError("Manifest case_file escapes the manifest directory")
    return case_path


def validate_dataset_manifest(path: str | Path) -> DatasetValidationResult:
    manifest_path = Path(path).resolve()
    manifest = load_manifest(manifest_path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        case_path = resolve_case_path(manifest_path, manifest)
    except ValueError as exc:
        return _empty_result(manifest_path, manifest, [str(exc)])
    if not case_path.is_file():
        return _empty_result(
            manifest_path, manifest, [f"Case file does not exist: {manifest.case_file}"]
        )
    raw = case_path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != manifest.case_file_sha256:
        errors.append("case_file_sha256 does not match the case file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _empty_result(
            manifest_path,
            manifest,
            [*errors, f"Case file is not valid UTF-8: {exc}"],
            actual_hash,
        )
    if _SECRET_PATTERN.search(text):
        errors.append("Dataset contains a suspected API key or bearer token")
    try:
        cases = load_cases(case_path)
    except ValueError as exc:
        return _empty_result(manifest_path, manifest, [*errors, str(exc)], actual_hash)
    _validate_cases(cases, errors)
    tags = Counter(tag for case in cases for tag in case.tags)
    tasks = Counter(case.task_type for case in cases if case.task_type)
    semantic = sum(case.oracle_type == "semantic_gold" for case in cases)
    behavioral = sum(case.oracle_type == "behavioral_contract" for case in cases)
    smoke = sum(case.oracle_type == "smoke" for case in cases)
    ratio = semantic / len(cases)
    evidence_issues: list[str] = []
    if len(cases) < manifest.minimum_cases:
        evidence_issues.append(
            f"case_count {len(cases)} is below minimum_cases {manifest.minimum_cases}"
        )
    if ratio < manifest.minimum_semantic_gold_ratio:
        evidence_issues.append(
            "semantic_gold_ratio "
            f"{ratio:.4f} is below required {manifest.minimum_semantic_gold_ratio:.4f}"
        )
    for tag in manifest.required_tags:
        if tags[tag] == 0:
            evidence_issues.append(f"required tag is missing: {tag}")
    if manifest.status == "approved":
        if not manifest.reviewer or not manifest.reviewed_at:
            errors.append("approved dataset requires reviewer and reviewed_at")
        errors.extend(evidence_issues)
    else:
        warnings.extend(evidence_issues)
        if manifest.status == "retired":
            warnings.append("dataset is retired and cannot be used for a release gate")
    result = DatasetValidationResult(
        valid=not errors,
        manifest_path=str(manifest_path),
        dataset_id=manifest.dataset_id,
        version=manifest.version,
        status=manifest.status,
        dataset_fingerprint=dataset_fingerprint(cases),
        case_file_sha256=actual_hash,
        case_count=len(cases),
        semantic_gold_cases=semantic,
        behavioral_contract_cases=behavioral,
        smoke_cases=smoke,
        semantic_gold_ratio=round(ratio, 4),
        blocker_cases=sum(case.criticality == "blocker" for case in cases),
        critical_cases=sum(case.criticality == "critical" for case in cases),
        tag_counts=dict(sorted(tags.items())),
        task_type_counts=dict(sorted(tasks.items())),
        errors=errors,
        warnings=warnings,
    )
    return result


def load_validated_dataset(
    manifest_path: str | Path,
) -> tuple[EvaluationDatasetManifest, list[EvaluationCase], DatasetValidationResult]:
    manifest = load_manifest(manifest_path)
    validation = validate_dataset_manifest(manifest_path)
    if not validation.valid:
        raise ValueError("Invalid evaluation dataset: " + "; ".join(validation.errors))
    cases = load_cases(resolve_case_path(manifest_path, manifest))
    return manifest, cases, validation


def validation_to_markdown(result: DatasetValidationResult) -> str:
    lines = [
        f"# Dataset validation: {result.dataset_id or 'unknown'}",
        "",
        f"- Status: **{'valid' if result.valid else 'invalid'}**",
        f"- Version: `{result.version}`",
        f"- Dataset state: `{result.status}`",
        f"- Cases: {result.case_count}",
        f"- Semantic Gold: {result.semantic_gold_cases} ({result.semantic_gold_ratio:.2%})",
        f"- Behavioral contracts: {result.behavioral_contract_cases}",
        f"- Smoke cases: {result.smoke_cases}",
        "",
        "## Errors",
        "",
    ]
    lines.extend(f"- {item}" for item in result.errors)
    if not result.errors:
        lines.append("None.")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {item}" for item in result.warnings)
    if not result.warnings:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _validate_cases(cases: list[EvaluationCase], errors: list[str]) -> None:
    for case in cases:
        prefix = f"case {case.case_id}:"
        if not case.tags:
            errors.append(f"{prefix} at least one tag is required")
        if not case.task_type:
            errors.append(f"{prefix} task_type is required")
        has_semantic = bool(
            case.required_facts
            or case.required_source_patterns
            or case.forbidden_claims
        )
        has_behavior = bool(
            case.expected_status is not None
            or case.expected_exploration != "either"
            or case.expected_persistence != "either"
            or case.max_elapsed_seconds is not None
            or case.max_pages_fetched is not None
        )
        if case.oracle_type == "semantic_gold" and not has_semantic:
            errors.append(f"{prefix} semantic_gold has no semantic oracle")
        if case.oracle_type == "behavioral_contract" and not has_behavior:
            errors.append(f"{prefix} behavioral_contract has no behavior oracle")
        if case.oracle_type == "smoke" and (has_semantic or has_behavior):
            errors.append(f"{prefix} smoke case must not contain release assertions")
        if case.criticality == "blocker" and (
            case.oracle_type != "semantic_gold" or case.expected_status is None
        ):
            errors.append(
                f"{prefix} blocker requires semantic_gold and expected_status"
            )
        for index, alternatives in enumerate(case.required_facts):
            if not alternatives or any(not term.strip() for term in alternatives):
                errors.append(f"{prefix} required_facts[{index}] is empty")


def _empty_result(
    path: Path,
    manifest: EvaluationDatasetManifest,
    errors: list[str],
    actual_hash: str = "",
) -> DatasetValidationResult:
    return DatasetValidationResult(
        valid=False,
        manifest_path=str(path),
        dataset_id=manifest.dataset_id,
        version=manifest.version,
        status=manifest.status,
        case_file_sha256=actual_hash,
        errors=errors,
    )
