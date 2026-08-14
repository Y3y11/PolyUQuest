"""Read-only drift planning and explicitly confirmed repair execution."""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

import structlog

from agent_rag.indexing.outbox import IndexOutbox, index_outbox
from agent_rag.knowledge import FactVersionStore, fact_version_store
from agent_rag.reconciliation.models import (
    ConsistencyFinding,
    ConsistencyInventory,
    ReconciliationRun,
    ReconciliationRunDetail,
    RepairAction,
)
from agent_rag.reconciliation.store import ReconciliationStore, reconciliation_store
from agent_rag.telemetry import TelemetryRecorder, telemetry_recorder
from agent_rag.tools.graph_patch import PublishPatchTool
from agent_rag.tools.observations import (
    ObservationStore,
    PatchStore,
    observation_store,
    patch_store,
)
from agent_rag.tools.schemas import PublishPatchInput
from agent_rag.versioning import PageVersionStore, page_version_store

logger = structlog.get_logger(__name__)


class ReconciliationService:
    def __init__(
        self,
        *,
        store: ReconciliationStore = reconciliation_store,
        outbox: IndexOutbox = index_outbox,
        versions: PageVersionStore = page_version_store,
        facts: FactVersionStore = fact_version_store,
        patches: PatchStore = patch_store,
        observations: ObservationStore = observation_store,
        inventory_factory: Callable[[], ConsistencyInventory] | None = None,
        publish_factory: Callable[[], PublishPatchTool] = PublishPatchTool,
        telemetry: TelemetryRecorder = telemetry_recorder,
    ):
        self.store = store
        self.outbox = outbox
        self.versions = versions
        self.facts = facts
        self.patches = patches
        self.observations = observations
        self.inventory_factory = inventory_factory
        self.publish_factory = publish_factory
        self.telemetry = telemetry

    def scan(
        self, *, verification_of_run_id: str | None = None
    ) -> ReconciliationRunDetail:
        run = self.store.create(
            ReconciliationRun(verification_of_run_id=verification_of_run_id)
        )
        telemetry_started = time.perf_counter()
        self.telemetry.start_run(
            run.run_id,
            "reconciliation",
            root_run_id=verification_of_run_id or run.run_id,
            parent_run_id=verification_of_run_id,
            attributes={"operation": "scan", "dry_run": True},
        )
        try:
            inventory = self._inventory()
            findings = self._findings(run.run_id, inventory)
            actions = self._actions(findings)
            categories = Counter(item.category for item in findings)
            run.summary = {
                "categories": dict(sorted(categories.items())),
                "neo4j_counts": {
                    key: len(value)
                    for key, value in inventory.neo4j_ids.items()
                },
                "qdrant_counts": {
                    key: len(value)
                    for key, value in inventory.qdrant_ids.items()
                },
                "dry_run": True,
            }
            detail = self.store.save_plan(run, findings, actions)
            self.telemetry.finish_run(
                run.run_id,
                telemetry_started,
                status="completed",
                response_status="planned",
                attributes={
                    "operation": "scan",
                    "findings_count": len(findings),
                    "actions_count": len(actions),
                },
            )
            return detail
        except Exception as exc:
            self.store.mark_scan_failed(run, str(exc))
            self.telemetry.finish_run(
                run.run_id,
                telemetry_started,
                status="error",
                response_status="scan_failed",
                error_category=exc.__class__.__name__,
            )
            logger.exception("reconciliation_scan_failed", run_id=run.run_id)
            raise

    def execute(self, run_id: str, *, confirmed: bool) -> ReconciliationRunDetail:
        if not confirmed:
            raise ValueError("Reconciliation execution requires confirm=true")
        detail = self.store.get_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        owner_id = f"recon-worker-{uuid.uuid4().hex}"
        telemetry_run_id = f"recon-exec-{uuid.uuid4().hex}"
        self.store.claim_execution(run_id, owner_id)
        self.store.recover_stale_actions(run_id)
        detail = self.store.get_detail(run_id)  # reload reclaimed actions
        telemetry_started = time.perf_counter()
        self.telemetry.start_run(
            telemetry_run_id,
            "reconciliation",
            root_run_id=run_id,
            parent_run_id=run_id,
            attributes={"operation": "execute", "plan_run_id": run_id},
        )
        heartbeat_stop = threading.Event()
        heartbeat_error: list[Exception] = []

        def _heartbeat() -> None:
            while not heartbeat_stop.wait(60.0):
                try:
                    self.store.heartbeat_execution(run_id, owner_id)
                except Exception as exc:
                    heartbeat_error.append(exc)
                    return

        heartbeat = threading.Thread(
            target=_heartbeat,
            name=f"reconciliation-heartbeat-{run_id[-8:]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            publisher: PublishPatchTool | None = None
            for planned in detail.actions:
                if heartbeat_error:
                    raise RuntimeError(
                        f"Reconciliation execution lease lost: {heartbeat_error[-1]}"
                    ) from heartbeat_error[-1]
                action = self.store.claim_action(planned.action_id, owner_id)
                if action is None:
                    continue
                try:
                    if action.action_type == "replay_patch":
                        publisher = publisher or self.publish_factory()
                        with self.telemetry.bind(telemetry_run_id):
                            self._replay_patch(action, publisher)
                    else:
                        self._retry_job(action)
                except Exception as exc:
                    action.status = "failed"
                    action.error = str(exc)[:4000]
                self.store.finish_action(action, owner_id)
            if heartbeat_error:
                raise RuntimeError(
                    f"Reconciliation execution lease lost: {heartbeat_error[-1]}"
                ) from heartbeat_error[-1]
            self.store.finish_execution(run_id, owner_id)
            try:
                verification = self.scan(verification_of_run_id=run_id)
                result = self.store.attach_verification(
                    run_id,
                    verification_run_id=verification.run.run_id,
                    findings_count=verification.run.findings_count,
                )
                self.telemetry.finish_run(
                    telemetry_run_id,
                    telemetry_started,
                    status="completed",
                    response_status="verified",
                    attributes={
                        "operation": "execute",
                        "verification_run_id": verification.run.run_id,
                        "remaining_findings": verification.run.findings_count,
                    },
                )
                return result
            except Exception as verification_exc:
                logger.warning(
                    "reconciliation_verification_failed",
                    run_id=run_id,
                    error=str(verification_exc),
                )
                result = self.store.attach_verification(
                    run_id,
                    verification_run_id=None,
                    error=str(verification_exc),
                )
                self.telemetry.finish_run(
                    telemetry_run_id,
                    telemetry_started,
                    status="completed",
                    response_status="verification_failed",
                    error_category=verification_exc.__class__.__name__,
                )
                return result
        except Exception as exc:
            self.telemetry.finish_run(
                telemetry_run_id,
                telemetry_started,
                status="error",
                response_status="execution_failed",
                error_category=exc.__class__.__name__,
            )
            try:
                self.store.fail_execution(run_id, owner_id, str(exc))
            except ValueError:
                logger.warning(
                    "reconciliation_execution_lease_lost",
                    run_id=run_id,
                    owner_id=owner_id,
                    error=str(exc),
                )
            raise
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2.0)

    def _inventory(self) -> ConsistencyInventory:
        if self.inventory_factory is not None:
            return self.inventory_factory()
        from agent_rag.storage.graph_vector_store import GraphVectorStore

        graph = GraphVectorStore()
        try:
            return graph.collect_reconciliation_inventory()
        finally:
            graph.close()

    def _findings(
        self, run_id: str, inventory: ConsistencyInventory
    ) -> list[ConsistencyFinding]:
        findings: list[ConsistencyFinding] = []
        patch_job_status = {
            job.patch_id: job.status
            for job in self.outbox.list(limit=10000)
            if job.status != "succeeded"
        }
        for object_type in ("webpages", "blocks", "entities", "relations"):
            neo_ids = inventory.neo4j_ids.get(object_type, set())
            qdrant_ids = inventory.qdrant_ids.get(object_type, set())
            missing_qdrant = neo_ids - qdrant_ids
            if object_type == "webpages":
                missing_qdrant -= inventory.non_vector_webpage_ids
            for object_id in sorted(missing_qdrant):
                hint = inventory.neo4j_patch_ids.get(object_id, {})
                if object_type == "relations" and not hint.get("patch_id"):
                    state = inventory.neo4j_facts.get(object_id)
                    version = (
                        self.versions.get(state.page_version_id)
                        if state is not None and state.page_version_id
                        else None
                    )
                    if version is not None:
                        hint = {
                            "patch_id": version.patch_id,
                            "source_url": version.source_url,
                        }
                findings.append(
                    self._storage_finding(
                        run_id,
                        object_type,
                        object_id,
                        expected_store="neo4j+qdrant",
                        actual_store="neo4j",
                        hint=hint,
                        patch_job_status=patch_job_status,
                    )
                )
            for object_id in sorted(qdrant_ids - neo_ids):
                findings.append(
                    self._storage_finding(
                        run_id,
                        object_type,
                        object_id,
                        expected_store="neo4j+qdrant",
                        actual_store="qdrant",
                        hint=inventory.qdrant_patch_ids.get(object_id, {}),
                        patch_job_status=patch_job_status,
                    )
                )

        for fact_key in sorted(
            set(inventory.neo4j_facts) & set(inventory.qdrant_fact_sources)
        ):
            neo_sources = sorted(inventory.neo4j_facts[fact_key].source_block_ids)
            qdrant_sources = sorted(inventory.qdrant_fact_sources[fact_key])
            if neo_sources == qdrant_sources:
                continue
            version_id = inventory.neo4j_facts[fact_key].page_version_id
            version = self.versions.get(version_id) if version_id else None
            patch_id = version.patch_id if version else ""
            findings.append(
                self._repairable_finding(
                    run_id=run_id,
                    category="relation_provenance_mismatch",
                    severity="critical",
                    object_type="relation",
                    object_id=fact_key,
                    patch_id=patch_id,
                    version_id=version_id,
                    expected={"source_block_ids": neo_sources},
                    actual={"source_block_ids": qdrant_sources},
                    reason="Neo4j and Qdrant relation evidence sets differ",
                    patch_job_status=patch_job_status,
                )
            )

        active_facts = self.facts.active_map()
        graph_fact_ids = set(inventory.neo4j_facts)
        for fact_key in sorted(set(active_facts) - graph_fact_ids):
            fact = active_facts[fact_key]
            version = self.versions.get(fact.introduced_version_id)
            patch_id = version.patch_id if version else ""
            findings.append(
                self._repairable_finding(
                    run_id=run_id,
                    category="active_fact_missing_from_graph",
                    severity="critical",
                    object_type="relation",
                    object_id=fact_key,
                    patch_id=patch_id,
                    version_id=fact.introduced_version_id,
                    source_url=fact.source_url,
                    expected={"status": "active"},
                    actual={"neo4j": "missing"},
                    reason="SQLite active fact is absent from Neo4j current state",
                    patch_job_status=patch_job_status,
                )
            )
        for fact_key in sorted(graph_fact_ids - set(active_facts)):
            state = inventory.neo4j_facts[fact_key]
            # Legacy offline facts predate FactVersion. Only online facts claim
            # page-version provenance and therefore constitute a ledger drift.
            if not state.page_version_id:
                continue
            findings.append(
                ConsistencyFinding(
                    run_id=run_id,
                    category="online_fact_missing_from_ledger",
                    severity="critical",
                    object_type="relation",
                    object_id=fact_key,
                    version_id=state.page_version_id,
                    expected={"sqlite_active": True},
                    actual={"sqlite_active": False},
                    reason="Online Neo4j fact has no active FactVersion ledger row",
                    repairability="manual_review",
                    recommended_action="Review fact history before replaying its patch",
                )
            )

        repair_versions = self.versions.list(
            status="repair_required", limit=10000
        )
        for version in repair_versions:
            findings.append(
                self._repairable_finding(
                    run_id=run_id,
                    category="page_version_repair_required",
                    severity="critical",
                    object_type="page_version",
                    object_id=version.version_id,
                    patch_id=version.patch_id,
                    version_id=version.version_id,
                    source_url=version.source_url,
                    expected={"status": "published"},
                    actual={"status": version.status, "error": version.error},
                    reason="PageVersion did not complete its verified publish",
                    patch_job_status=patch_job_status,
                )
            )
        version_patch_ids = {item.patch_id for item in repair_versions}
        for patch in self.patches.list_by_status(
            ("publishing", "repair_required"), limit=10000
        ):
            if patch.patch_id in version_patch_ids:
                continue
            findings.append(
                self._repairable_finding(
                    run_id=run_id,
                    category="graph_patch_incomplete",
                    severity="critical",
                    object_type="graph_patch",
                    object_id=patch.patch_id,
                    patch_id=patch.patch_id,
                    source_url=patch.source_url,
                    expected={"status": "published"},
                    actual={"status": patch.status, "error": patch.error},
                    reason="GraphPatch did not reach a verified published state",
                    patch_job_status=patch_job_status,
                )
            )
        for job in self.outbox.list(status="dead_letter", limit=10000):
            findings.append(
                ConsistencyFinding(
                    run_id=run_id,
                    category="index_job_dead_letter",
                    severity="critical",
                    object_type="index_job",
                    object_id=job.job_id,
                    source_url=job.source_url,
                    patch_id=job.patch_id,
                    job_id=job.job_id,
                    expected={"status": "succeeded"},
                    actual={"status": job.status, "last_error": job.last_error},
                    reason="Index job exhausted its automatic retry budget",
                    repairability="automatic",
                    recommended_action="retry_index_job",
                )
            )
        return findings

    def _storage_finding(
        self,
        run_id: str,
        object_type: str,
        object_id: str,
        *,
        expected_store: str,
        actual_store: str,
        hint: dict[str, str],
        patch_job_status: dict[str, str],
    ) -> ConsistencyFinding:
        patch_id = hint.get("patch_id", "")
        return self._repairable_finding(
            run_id=run_id,
            category=f"{object_type}_store_drift",
            severity="warning" if object_type == "entities" else "critical",
            object_type=object_type.rstrip("s"),
            object_id=object_id,
            patch_id=patch_id,
            source_url=hint.get("source_url", ""),
            expected={"stores": expected_store},
            actual={"stores": actual_store},
            reason=f"{object_type} object exists in only one current-state store",
            patch_job_status=patch_job_status,
        )

    def _repairable_finding(
        self,
        *,
        run_id: str,
        category: str,
        severity: str,
        object_type: str,
        object_id: str,
        patch_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
        reason: str,
        patch_job_status: dict[str, str],
        source_url: str = "",
        version_id: str = "",
    ) -> ConsistencyFinding:
        patch = self.patches.get(patch_id) if patch_id else None
        observation = (
            self.observations.get(patch.observation_id) if patch is not None else None
        )
        latest_version = (
            self.versions.get_latest(patch.source_url) if patch is not None else None
        )
        job_status = patch_job_status.get(patch_id)
        if job_status in {"pending", "running", "retry"}:
            repairability = "informational"
            action = f"Wait for the {job_status} index job"
        elif job_status == "dead_letter":
            repairability = "informational"
            action = "Use the index_job_dead_letter retry action"
        elif latest_version is not None and latest_version.patch_id != patch_id:
            repairability = "manual_review"
            action = (
                "Do not replay a historical snapshot; inspect the latest "
                f"PageVersion patch {latest_version.patch_id}"
            )
        elif patch is not None and observation is not None:
            repairability = "automatic"
            action = "replay_patch"
        else:
            repairability = "manual_review"
            action = "Locate a durable Observation/PageVersion before repair"
        return ConsistencyFinding(
            run_id=run_id,
            category=category,
            severity=severity,  # type: ignore[arg-type]
            object_type=object_type,
            object_id=object_id,
            source_url=source_url or (patch.source_url if patch else ""),
            patch_id=patch_id,
            version_id=version_id,
            expected=expected,
            actual=actual,
            reason=reason,
            repairability=repairability,
            recommended_action=action,
        )

    @staticmethod
    def _actions(findings: list[ConsistencyFinding]) -> list[RepairAction]:
        actions: list[RepairAction] = []
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            if finding.repairability != "automatic":
                continue
            if finding.recommended_action == "replay_patch":
                action_type, target_id = "replay_patch", finding.patch_id
            elif finding.recommended_action == "retry_index_job":
                action_type, target_id = "retry_index_job", finding.job_id
            else:
                continue
            key = (action_type, target_id)
            if not target_id or key in seen:
                continue
            seen.add(key)
            actions.append(
                RepairAction(
                    run_id=finding.run_id,
                    finding_id=finding.finding_id,
                    action_type=action_type,  # type: ignore[arg-type]
                    target_id=target_id,
                )
            )
        return actions

    def _replay_patch(
        self, action: RepairAction, publisher: PublishPatchTool
    ) -> None:
        job = next(
            (
                item for item in self.outbox.list(status="running", limit=10000)
                if item.patch_id == action.target_id
            ),
            None,
        )
        if job is not None:
            action.status = "skipped"
            action.before = {"job_id": job.job_id, "status": job.status}
            action.after = {"reason": "patch_has_running_index_job"}
            return
        patch = self.patches.get(action.target_id)
        if patch is None:
            action.status = "failed"
            action.error = "Patch no longer exists"
            return
        latest_version = self.versions.get_latest(patch.source_url)
        if latest_version is not None and latest_version.patch_id != patch.patch_id:
            action.status = "skipped"
            action.before = {
                "patch_status": patch.status,
                "latest_patch_id": latest_version.patch_id,
            }
            action.after = {"reason": "historical_patch_no_longer_current"}
            return
        action.before = {"patch_status": patch.status, "attempts": patch.attempts}
        if patch.status == "published":
            # The normal publisher intentionally fast-paths published patches.
            # A confirmed repair must reopen it so the persisted PageVersion
            # and KnowledgeDelta are idempotently replayed and verified.
            from agent_rag.reconciliation.models import utc_now

            patch.status = "repair_required"
            patch.error = "Reopened by confirmed reconciliation repair"
            patch.updated_at = utc_now()
            self.patches.put(patch)
        output = publisher.run(PublishPatchInput(patch_id=action.target_id))
        action.after = {
            "patch_status": output.patch.status,
            "read_after_write_ok": output.read_after_write_ok,
            "version_id": output.version_id,
        }
        action.status = (
            "succeeded"
            if output.patch.status == "published" and output.read_after_write_ok
            else "failed"
        )
        if action.status == "failed":
            action.error = output.patch.error or "Patch replay did not verify"

    def _retry_job(self, action: RepairAction) -> None:
        job = self.outbox.get(action.target_id)
        if job is None:
            action.status = "failed"
            action.error = "Index job no longer exists"
            return
        action.before = {"status": job.status, "manual_retries": job.manual_retries}
        if job.status not in {"retry", "dead_letter"}:
            action.status = "skipped"
            action.after = {"status": job.status, "reason": "job_state_changed"}
            return
        retried = self.outbox.retry(job.job_id)
        action.status = "succeeded"
        action.after = {
            "status": retried.status,
            "manual_retries": retried.manual_retries,
        }


reconciliation_service = ReconciliationService()
