"""Human-readable evaluation report rendering."""

from __future__ import annotations

from agent_rag.evaluation.models import EvaluationReport


def to_markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Evaluation: {report.variant}",
        "",
        f"- Cases: {report.cases}",
        f"- Dataset: `{report.dataset_fingerprint}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for metric, value in report.summary.items():
        lines.append(f"| {metric} | {'N/A' if value is None else value} |")
    lines.extend(["", "## Failed cases", ""])
    failed = [item for item in report.results if item.failures]
    if not failed:
        lines.append("None.")
    else:
        lines.extend(f"- `{item.case_id}`: {', '.join(item.failures)}" for item in failed)
    return "\n".join(lines) + "\n"
