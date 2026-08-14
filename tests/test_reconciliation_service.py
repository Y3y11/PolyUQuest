from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.knowledge import FactVersionStore
from agent_rag.reconciliation import (
    ConsistencyInventory,
    CurrentFactState,
    ReconciliationStore,
)
from agent_rag.reconciliation.service import ReconciliationService
from agent_rag.tools.observations import ObservationRecord, ObservationStore, PatchStore
from agent_rag.tools.schemas import GraphPatch, PublishPatchOutput
from agent_rag.versioning import PageVersionStore
from agent_rag.versioning.diff import BlockDiffPlan
from agent_rag.versioning.store import PageVersion


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _patch(patch_id: str = "patch-recon") -> GraphPatch:
    return GraphPatch(
        patch_id=patch_id,
        observation_id="obs-recon",
        run_id="agent-run",
        source_url="https://example.org/guide",
        content_hash="hash-recon",
        status="repair_required",
        created_at=_now(),
        updated_at=_now(),
    )


def _service(temp_dir: str, inventory: ConsistencyInventory):
    path = Path(temp_dir) / "ledger.sqlite3"
    stores = {
        "store": ReconciliationStore(path),
        "outbox": IndexOutbox(path),
        "versions": PageVersionStore(path),
        "facts": FactVersionStore(path),
        "patches": PatchStore(db_path=path),
        "observations": ObservationStore(db_path=path),
    }
    service = ReconciliationService(
        **stores,
        inventory_factory=lambda: inventory,
    )
    return service, stores


def test_scan_is_read_only_deduplicates_replay_and_marks_unknown_manual() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={
            "webpages": set(),
            "blocks": {"block-known", "block-unknown"},
            "entities": set(),
            "relations": {"fact-1"},
        },
        qdrant_ids={
            "webpages": set(), "blocks": set(), "entities": set(), "relations": {"fact-1"}
        },
        neo4j_patch_ids={
            "block-known": {"patch_id": "patch-recon", "source_url": "https://example.org/guide"},
        },
        neo4j_facts={
            "fact-1": CurrentFactState(
                fact_key="fact-1", source_block_ids=["block-new"]
            )
        },
        qdrant_fact_sources={"fact-1": ["block-old"]},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        patch = _patch()
        stores["patches"].put(patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=patch.observation_id,
                run_id=patch.run_id,
                raw_html="<p>guide</p>",
                metadata={"url": patch.source_url, "content_hash": patch.content_hash},
                blocks=[{"block_id": "block-known", "content": "guide"}],
            )
        )

        detail = service.scan()

        assert detail.run.status == "planned"
        assert detail.run.findings_count == 4
        assert len(detail.actions) == 1
        assert detail.actions[0].target_id == "patch-recon"
        unknown = next(
            item for item in detail.findings if item.object_id == "block-unknown"
        )
        assert unknown.repairability == "manual_review"
        assert stores["patches"].get(patch.patch_id).status == "repair_required"


def test_inflight_patch_is_not_planned_for_concurrent_replay() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={"blocks": {"block-1"}},
        qdrant_ids={"blocks": set()},
        neo4j_patch_ids={"block-1": {"patch_id": "patch-recon"}},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        patch = _patch()
        stores["patches"].put(patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=patch.observation_id,
                run_id=patch.run_id,
                raw_html="x",
                metadata={"url": patch.source_url, "content_hash": patch.content_hash},
                blocks=[{"block_id": "block-1", "content": "x"}],
            )
        )
        stores["outbox"].enqueue(patch)
        stores["outbox"].claim("worker-live")

        detail = service.scan()

        assert detail.actions == []
        assert detail.findings[0].repairability == "informational"


def test_confirmed_execution_retries_dead_letter_and_replays_patch() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={"blocks": {"block-1"}},
        qdrant_ids={"blocks": set()},
        neo4j_patch_ids={"block-1": {"patch_id": "patch-recon"}},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        patch = _patch()
        stores["patches"].put(patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=patch.observation_id,
                run_id=patch.run_id,
                raw_html="x",
                metadata={"url": patch.source_url, "content_hash": patch.content_hash},
                blocks=[{"block_id": "block-1", "content": "x"}],
            )
        )
        job, _ = stores["outbox"].enqueue(_patch("patch-dead"), max_attempts=1)
        stores["outbox"].claim("worker-dead")
        stores["outbox"].fail(job.job_id, "worker-dead", "boom")

        calls: list[str] = []

        class Publisher:
            def run(self, tool_input):
                calls.append(tool_input.patch_id)
                current = stores["patches"].get(tool_input.patch_id)
                current.status = "published"
                current.error = None
                stores["patches"].put(current)
                return PublishPatchOutput(
                    patch=current, read_after_write_ok=True
                )

        service.publish_factory = Publisher
        detail = service.scan()
        assert len(detail.actions) == 2
        with pytest.raises(ValueError):
            service.execute(detail.run.run_id, confirmed=False)

        executed = service.execute(detail.run.run_id, confirmed=True)

        assert executed.run.status == "completed"
        assert executed.run.succeeded_count == 2
        assert calls == ["patch-recon"]
        assert stores["outbox"].get(job.job_id).status == "pending"
        verification_id = executed.run.summary["verification_run_id"]
        verification = stores["store"].get(verification_id)
        assert verification.verification_of_run_id == detail.run.run_id


def test_dead_letter_for_same_patch_suppresses_direct_replay() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={"blocks": {"block-1"}},
        qdrant_ids={"blocks": set()},
        neo4j_patch_ids={"block-1": {"patch_id": "patch-recon"}},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        patch = _patch()
        stores["patches"].put(patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=patch.observation_id,
                run_id=patch.run_id,
                raw_html="x",
                metadata={"url": patch.source_url, "content_hash": patch.content_hash},
                blocks=[{"block_id": "block-1", "content": "x"}],
            )
        )
        job, _ = stores["outbox"].enqueue(patch, max_attempts=1)
        stores["outbox"].claim("worker-dead")
        stores["outbox"].fail(job.job_id, "worker-dead", "boom")

        detail = service.scan()

        assert len(detail.actions) == 1
        assert detail.actions[0].action_type == "retry_index_job"
        drift = next(item for item in detail.findings if item.object_id == "block-1")
        assert drift.repairability == "informational"


def test_replay_skips_when_job_becomes_running_after_scan() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={"blocks": {"block-1"}},
        qdrant_ids={"blocks": set()},
        neo4j_patch_ids={"block-1": {"patch_id": "patch-recon"}},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        patch = _patch()
        stores["patches"].put(patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=patch.observation_id,
                run_id=patch.run_id,
                raw_html="x",
                metadata={"url": patch.source_url, "content_hash": patch.content_hash},
                blocks=[{"block_id": "block-1", "content": "x"}],
            )
        )
        detail = service.scan()
        stores["outbox"].enqueue(patch)
        stores["outbox"].claim("worker-race")

        executed = service.execute(detail.run.run_id, confirmed=True)

        assert executed.run.skipped_count == 1
        assert executed.actions[0].after["reason"] == "patch_has_running_index_job"


def test_historical_patch_is_not_replayed_over_latest_page_version() -> None:
    inventory = ConsistencyInventory(
        neo4j_ids={"blocks": {"block-old"}},
        qdrant_ids={"blocks": set()},
        neo4j_patch_ids={"block-old": {"patch_id": "patch-old"}},
    )
    with TemporaryDirectory() as temp_dir:
        service, stores = _service(temp_dir, inventory)
        old_patch = _patch("patch-old")
        stores["patches"].put(old_patch)
        stores["observations"].put(
            ObservationRecord(
                observation_id=old_patch.observation_id,
                run_id=old_patch.run_id,
                raw_html="old",
                metadata={
                    "url": old_patch.source_url,
                    "content_hash": old_patch.content_hash,
                },
                blocks=[{"block_id": "block-old", "content": "old"}],
            )
        )
        latest = PageVersion(
            patch_id="patch-latest",
            observation_id="obs-latest",
            run_id="run-latest",
            source_url=old_patch.source_url,
            content_hash="latest-hash",
            status="published",
            diff=BlockDiffPlan(),
        )
        stores["versions"].put_planned(latest)
        stores["versions"].save(latest)

        detail = service.scan()

        assert detail.actions == []
        finding = next(item for item in detail.findings if item.object_id == "block-old")
        assert finding.repairability == "manual_review"
        assert "historical snapshot" in finding.recommended_action
