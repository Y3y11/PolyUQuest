"""CLI entry point for the deterministic business E2E gate."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/runtime/business-e2e/report.json"),
    )
    parser.add_argument("--runtime-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # This diagnostic mode is an existing production retrieval option. Setting
    # it before importing the scenario prevents a remote reranker call without
    # changing production defaults.
    os.environ.setdefault("RERANKER_MODE", "first_stage_only")
    from agent_rag.e2e.scenario import run_business_e2e

    try:
        report = run_business_e2e(
            output=args.output,
            runtime_dir=args.runtime_dir,
        )
    except BaseException as exc:
        print(f"Business E2E failed: {type(exc).__name__}: {exc}")
        print(f"Evidence: {args.output.resolve()}")
        return 1
    print(
        f"Business E2E {report.status}: checks={len(report.checks)} "
        f"duration_ms={report.duration_ms}"
    )
    print(f"Evidence: {args.output.resolve()}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
