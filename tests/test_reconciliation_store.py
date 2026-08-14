from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_rag.reconciliation import (
    ConsistencyFinding,
    ReconciliationRun,
    ReconciliationStore,
    RepairAction,
)


def test_reconciliation_plan_survives_restart_and_actions_use_cas() -> None:
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "ledger.sqlite3"
        store = ReconciliationStore(path)
        run = store.create(ReconciliationRun())
        finding = ConsistencyFinding(
            run_id=run.run_id,
            category="block_store_drift",
            severity="critical",
            object_type="block",
            object_id="block-1",
            patch_id="patch-1",
            expected={"stores": "neo4j+qdrant"},
            actual={"stores": "neo4j"},
            reason="missing vector",
            repairability="automatic",
            recommended_action="replay_patch",
        )
        action = RepairAction(
            run_id=run.run_id,
            finding_id=finding.finding_id,
            action_type="replay_patch",
            target_id="patch-1",
        )
        store.save_plan(run, [finding], [action])

        restored = ReconciliationStore(path).get_detail(run.run_id)
        assert restored is not None
        assert restored.run.status == "planned"
        assert restored.findings[0].object_id == "block-1"
        assert restored.actions[0].target_id == "patch-1"

        store.claim_execution(run.run_id, "worker-1")
        assert store.claim_action(action.action_id, "worker-1").status == "running"
        assert store.claim_action(action.action_id, "worker-2") is None


def test_only_planned_run_can_be_claimed() -> None:
    with TemporaryDirectory() as temp_dir:
        store = ReconciliationStore(Path(temp_dir) / "ledger.sqlite3")
        run = store.create(ReconciliationRun())
        store.save_plan(run, [], [])
        store.claim_execution(run.run_id, "worker-1")
        with pytest.raises(ValueError):
            store.claim_execution(run.run_id, "worker-2")


def test_expired_execution_reclaims_running_action_without_stale_overwrite() -> None:
    with TemporaryDirectory() as temp_dir:
        store = ReconciliationStore(Path(temp_dir) / "ledger.sqlite3")
        run = store.create(ReconciliationRun())
        finding = ConsistencyFinding(
            run_id=run.run_id,
            category="block_store_drift",
            severity="critical",
            object_type="block",
            object_id="block-1",
            reason="missing vector",
            repairability="automatic",
            recommended_action="replay_patch",
        )
        action = RepairAction(
            run_id=run.run_id,
            finding_id=finding.finding_id,
            action_type="replay_patch",
            target_id="patch-1",
        )
        store.save_plan(run, [finding], [action])
        store.claim_execution(run.run_id, "worker-old")
        stale_action = store.claim_action(action.action_id, "worker-old")
        with store._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE reconciliation_runs SET execution_lease_until=? "
                "WHERE run_id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), run.run_id),
            )

        store.claim_execution(run.run_id, "worker-new")
        assert store.recover_stale_actions(run.run_id) == 1
        reclaimed = store.claim_action(action.action_id, "worker-new")
        assert reclaimed.execution_owner_id == "worker-new"

        stale_action.status = "succeeded"
        with pytest.raises(ValueError):
            store.finish_action(stale_action, "worker-old")
