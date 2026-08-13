"""Bounded startup recovery for interrupted graph patches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from agent_rag.config import settings
from agent_rag.tools.graph_patch import PublishPatchTool
from agent_rag.tools.observations import PatchStore, patch_store
from agent_rag.tools.schemas import PublishPatchInput

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RecoveryReport:
    scanned: int = 0
    recovered: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def recover_pending_patches(
    patches: PatchStore = patch_store,
    publish_factory: Callable[[], PublishPatchTool] = PublishPatchTool,
    *,
    limit: int | None = None,
    max_attempts: int | None = None,
) -> RecoveryReport:
    """Replay interrupted patches through the normal idempotent publisher."""
    report = RecoveryReport()
    limit = limit if limit is not None else settings.agent_repair_max_patches
    max_attempts = (
        max_attempts if max_attempts is not None else settings.agent_patch_max_attempts
    )
    candidates = patches.list_by_status(
        ("publishing", "repair_required"), limit=limit
    )
    report.scanned = len(candidates)
    if not candidates:
        return report

    publisher = publish_factory()
    for patch in candidates:
        if patch.attempts >= max_attempts:
            report.skipped += 1
            logger.warning(
                "patch_recovery_attempt_limit",
                patch_id=patch.patch_id,
                attempts=patch.attempts,
            )
            continue
        try:
            output = publisher.run(PublishPatchInput(patch_id=patch.patch_id))
            if output.patch.status == "published" and output.read_after_write_ok:
                report.recovered += 1
            else:
                report.failed += 1
                report.errors.append(
                    f"{patch.patch_id}: status={output.patch.status}"
                )
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"{patch.patch_id}: {exc}")
            logger.warning(
                "patch_recovery_failed", patch_id=patch.patch_id, error=str(exc)
            )
    return report
