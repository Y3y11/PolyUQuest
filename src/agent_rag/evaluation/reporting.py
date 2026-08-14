"""Human-readable evaluation report rendering."""

from __future__ import annotations

from agent_rag.evaluation.models import EvaluationReport


def to_markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Evaluation: {report.variant}",
        "",
        f"- Cases: {report.cases}",
        f"- Dataset: `{report.dataset_id or 'unmanaged'}@{report.dataset_version or 'N/A'}`",
        f"- Dataset state: `{report.dataset_status or 'unmanaged'}`",
        f"- Dataset fingerprint: `{report.dataset_fingerprint}`",
        f"- Evaluator: `{report.evaluator_version}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for metric, value in report.summary.items():
        lines.append(f"| {metric} | {'N/A' if value is None else value} |")
    lines.extend(
        [
            "",
            "## Business slices",
            "",
            "| Slice | Cases | Missing | Gold | Quality | Operational | Overall |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, summary in report.slices.items():
        lines.append(
            "| "
            + " | ".join(
                str(value)
                for value in (
                    name,
                    summary.case_count,
                    summary.missing_responses,
                    summary.semantic_gold_cases,
                    _display(summary.quality_score),
                    _display(summary.operational_score),
                    _display(summary.overall),
                )
            )
            + " |"
        )
    lines.extend(["", "## Failed cases", ""])
    failed = [item for item in report.results if item.failures]
    if not failed:
        lines.append("None.")
    else:
        lines.extend(f"- `{item.case_id}`: {', '.join(item.failures)}" for item in failed)
    return "\n".join(lines) + "\n"


def _display(value: float | None) -> str:
    return "N/A" if value is None else str(value)
