"""Real-store deterministic scenario for the online knowledge closed loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_rag.agent.frontier import FrontierSelector
from agent_rag.agent.orchestrator import QueryDrivenAgent
from agent_rag.agent.schemas import AgentBudget, AgentQueryRequest
from agent_rag.config import crawl_config, settings
from agent_rag.e2e.contract import BusinessE2EReport, ContractRecorder
from agent_rag.e2e.deterministic import (
    DeterministicAnswerComposer,
    DeterministicEmbedder,
    DeterministicKnowledgeExtractor,
    DeterministicLLM,
    DeterministicProfileEnricher,
)
from agent_rag.e2e.site import MappedFixtureFetcher, VersionedFixtureSite
from agent_rag.freshness import PageLifecycleStore
from agent_rag.freshness.worker import FreshnessWorker
from agent_rag.indexing.outbox import IndexOutbox
from agent_rag.indexing.worker import IndexWorker
from agent_rag.knowledge import FactVersionStore
from agent_rag.llm.client import snapshot_usage as snapshot_llm_usage
from agent_rag.quality import PageQualityGate, PageQualityStore
from agent_rag.retrieval._reranker import mode as reranker_mode
from agent_rag.retrieval._reranker import snapshot_usage as snapshot_reranker_usage
from agent_rag.storage.graph_vector_store import GraphVectorStore
from agent_rag.storage.neo4j_store import Neo4jStore
from agent_rag.telemetry import TelemetryRecorder, TelemetryStore
from agent_rag.tools.expand import ExpandTool
from agent_rag.tools.fetch import FetchTrustedPageTool
from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool
from agent_rag.tools.observations import ObservationStore, PatchStore
from agent_rag.tools.schemas import PublishPatchInput
from agent_rag.tools.search import SearchTool
from agent_rag.tools.snapshot import PageSnapshotTool
from agent_rag.versioning import PageVersionStore

_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _ROOT / "configs" / "business_e2e.yaml"


def _load_config() -> dict[str, Any]:
    payload = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if payload.get("schema_version") != 1:
        raise ValueError("business_e2e.yaml schema_version must be 1")
    return payload


def _usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


def _inventory_digest(store: GraphVectorStore) -> str:
    inventory = store.collect_reconciliation_inventory()
    payload = {
        "neo4j_ids": {
            key: sorted(values) for key, values in sorted(inventory.neo4j_ids.items())
        },
        "qdrant_ids": {
            key: sorted(values) for key, values in sorted(inventory.qdrant_ids.items())
        },
        "neo4j_facts": {
            key: value.model_dump(mode="json")
            for key, value in sorted(inventory.neo4j_facts.items())
        },
        "qdrant_fact_sources": {
            key: sorted(value)
            for key, value in sorted(inventory.qdrant_fact_sources.items())
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _OneShotFault:
    def __init__(self):
        self.remaining = 1
        self.mutation_attempts = 0


class _FailFirstQdrantMutation:
    def __init__(self, delegate: Any, fault: _OneShotFault):
        self._delegate = delegate
        self._fault = fault

    def upsert_points(self, *args: Any, **kwargs: Any) -> Any:
        self._fault.mutation_attempts += 1
        if self._fault.remaining:
            self._fault.remaining -= 1
            raise RuntimeError("business-e2e injected Qdrant mutation failure")
        return self._delegate.upsert_points(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _FaultingGraphFactory:
    def __init__(self, fault: _OneShotFault):
        self.fault = fault

    def __call__(self) -> GraphVectorStore:
        store = GraphVectorStore()
        if self.fault.remaining:
            store.qdrant = _FailFirstQdrantMutation(store.qdrant, self.fault)
        return store


@dataclass
class _Stores:
    observations: ObservationStore
    patches: PatchStore
    outbox: IndexOutbox
    lifecycle: PageLifecycleStore
    quality: PageQualityStore
    versions: PageVersionStore
    facts: FactVersionStore
    telemetry_store: TelemetryStore
    telemetry: TelemetryRecorder


def _stores(path: Path) -> _Stores:
    telemetry_store = TelemetryStore(path)
    return _Stores(
        observations=ObservationStore(max_records=2048, db_path=path),
        patches=PatchStore(max_records=2048, db_path=path),
        outbox=IndexOutbox(path),
        lifecycle=PageLifecycleStore(path),
        quality=PageQualityStore(path),
        versions=PageVersionStore(path),
        facts=FactVersionStore(path),
        telemetry_store=telemetry_store,
        telemetry=TelemetryRecorder(telemetry_store),
    )


def _wait_for_dependencies(config: dict[str, Any]) -> None:
    dependency = config.get("dependencies", {})
    deadline = time.monotonic() + float(dependency.get("startup_timeout_seconds", 120))
    interval = float(dependency.get("retry_interval_seconds", 2))
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        store: GraphVectorStore | None = None
        try:
            store = GraphVectorStore()
            store.init_all()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(interval)
        finally:
            if store is not None:
                store.close()
    raise RuntimeError(f"Neo4j/Qdrant did not become ready: {last_error}")


class BusinessE2EScenario:
    def __init__(self, runtime_dir: Path, report_path: Path):
        self.config = _load_config()
        self.runtime_dir = runtime_dir
        self.report_path = report_path
        token = f"e2e-{uuid.uuid4().hex[:10]}"
        self.token = token
        self.report = BusinessE2EReport(
            scenario_id=str(self.config["scenario_id"]),
            scenario_token=token,
            code_version=(
                os.getenv("GITHUB_SHA", "").strip()
                or os.getenv("CODE_VERSION", "").strip()
            ),
        )
        self.recorder = ContractRecorder(self.report)
        self.embedder = DeterministicEmbedder(settings.embedding_dim)
        self.extractor = DeterministicKnowledgeExtractor(token)
        fixture = self.config.get("fixture", {})
        path = f"{str(fixture.get('path_prefix', '/access/')).rstrip('/')}/{token}/"
        self.site = VersionedFixtureSite(
            token,
            str(fixture.get("canonical_host", "e2e.test")),
            path,
        )

    def _publisher(
        self,
        stores: _Stores,
        graph_factory: Any = GraphVectorStore,
    ) -> PublishPatchTool:
        return PublishPatchTool(
            observations=stores.observations,
            patches=stores.patches,
            graph_store_factory=graph_factory,
            embedder=self.embedder.embed_many,
            lifecycle_store=stores.lifecycle,
            version_store=stores.versions,
            extractor=self.extractor,
            fact_store=stores.facts,
        )

    def _agent(self, stores: _Stores, fetch_tool: FetchTrustedPageTool) -> QueryDrivenAgent:
        search = SearchTool(
            llm_factory=DeterministicLLM,
            embedder=self.embedder.embed_query,
            lifecycle_store=stores.lifecycle,
        )
        stage = StagePatchTool(stores.observations, stores.patches)
        return QueryDrivenAgent(
            search_tool=search,
            expand_tool=ExpandTool(),
            fetch_tool=fetch_tool,
            stage_patch_tool=stage,
            publish_patch_tool=self._publisher(stores),
            profile_enricher=DeterministicProfileEnricher(),
            frontier_selector=FrontierSelector(llm_factory=DeterministicLLM),
            composer=DeterministicAnswerComposer(),
            observations=stores.observations,
            snapshot_tool=PageSnapshotTool(),
            indexing_outbox=stores.outbox,
            quality_gate=PageQualityGate(),
            quality_store=stores.quality,
            lifecycle_store=stores.lifecycle,
            telemetry=stores.telemetry,
        )

    def _request(self, query: str) -> AgentQueryRequest:
        return AgentQueryRequest(
            query=query,
            mode="block",
            explore_web=True,
            persist_discoveries=True,
            budget=AgentBudget(
                max_iterations=2,
                max_pages=1,
                max_depth=2,
                max_seconds=60,
            ),
        )

    def run(self) -> BusinessE2EReport:
        error: BaseException | None = None
        original_crawl = dict(crawl_config)
        llm_before = snapshot_llm_usage()
        reranker_before = snapshot_reranker_usage()
        contract = self.config.get("contract", {})
        ledger = self.runtime_dir / f"{self.token}.sqlite3"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        query = (
            f"How does an employee request {self.token} production database access? "
            "Provide the steps and approval."
        )
        try:
            self.recorder.check(
                "configuration.async_indexing",
                settings.agent_async_indexing,
                expected=True,
                actual=settings.agent_async_indexing,
                required=True,
            )
            self.recorder.check(
                "configuration.reranker_is_deterministic",
                reranker_mode() == "first_stage_only",
                expected="first_stage_only",
                actual=reranker_mode(),
                required=True,
            )
            with self.recorder.stage("dependencies") as metrics:
                _wait_for_dependencies(self.config)
                metrics.update(
                    {
                        "neo4j_store": "Neo4jStore",
                        "qdrant_store": "QdrantStore",
                        "embedding_dimension": settings.embedding_dim,
                    }
                )

            crawl_config.update(
                {
                    "site_identity": "Deterministic Enterprise Knowledge Site",
                    "seed_urls": [self.site.canonical_url],
                    "seed_labels": {
                        self.site.canonical_url: "Production database access procedure"
                    },
                    "domain_whitelist": [self.site.canonical_host],
                }
            )
            with self.site:
                mapped_fetcher = MappedFixtureFetcher(self.site)
                stores = _stores(ledger)
                fetch_tool = FetchTrustedPageTool(
                    fetcher=mapped_fetcher,
                    store=stores.observations,
                )

                with self.recorder.stage("cold_start_query") as metrics:
                    cold = asyncio.run(self._agent(stores, fetch_tool).run(self._request(query)))
                    quality_stats = stores.quality.stats()
                    metrics.update(
                        {
                            "run_id": cold.run_id,
                            "response_status": cold.response_status,
                            "pages_fetched": cold.exploration.pages_fetched,
                            "jobs_queued": cold.exploration.indexing_jobs_queued,
                            "evidence": len(cold.evidence),
                            "quality": quality_stats,
                            "origin_requests": self.site.request_count,
                        }
                    )
                    self.report.audit_ids["cold_agent_run"] = cold.run_id
                    self.recorder.check(
                        "cold_start.answer_is_grounded",
                        cold.response_status == "answered"
                        and self.site.canonical_url in cold.answer,
                        expected="answered with canonical source",
                        actual=cold.response_status,
                        required=True,
                    )
                    self.recorder.check(
                        "cold_start.single_page_budget",
                        cold.exploration.pages_fetched
                        <= int(contract.get("max_initial_pages_fetched", 1)),
                        expected=f"<= {contract.get('max_initial_pages_fetched', 1)}",
                        actual=cold.exploration.pages_fetched,
                    )
                    self.recorder.check(
                        "cold_start.quality_indexed",
                        quality_stats.get("index") == 1,
                        expected=1,
                        actual=quality_stats.get("index"),
                        required=True,
                    )
                    self.recorder.check(
                        "cold_start.job_created",
                        stores.outbox.stats().get("pending")
                        == int(contract.get("expected_initial_jobs", 1)),
                        expected=contract.get("expected_initial_jobs", 1),
                        actual=stores.outbox.stats().get("pending"),
                        required=True,
                    )

                fault = _OneShotFault()
                faulting_factory = _FaultingGraphFactory(fault)
                with self.recorder.stage("partial_failure") as metrics:
                    failing_worker = IndexWorker(
                        stores.outbox,
                        publish_factory=lambda: self._publisher(stores, faulting_factory),
                        worker_id="business-e2e-fault",
                        retry_base_seconds=0.0,
                        retry_max_seconds=0.0,
                        telemetry=stores.telemetry,
                    )
                    failed_job = failing_worker.process_once()
                    if failed_job is None:
                        raise RuntimeError("Expected a pending index job")
                    failed_patch = stores.patches.get(failed_job.patch_id)
                    with Neo4jStore() as graph:
                        partial_page = graph.get_webpages_batch([self.site.canonical_url]).get(
                            self.site.canonical_url
                        )
                    metrics.update(
                        {
                            "job_id": failed_job.job_id,
                            "job_status": failed_job.status,
                            "patch_status": failed_patch.status if failed_patch else "missing",
                            "qdrant_mutation_attempts": fault.mutation_attempts,
                            "partial_neo4j_page": bool(partial_page),
                        }
                    )
                    self.report.audit_ids["initial_job"] = failed_job.job_id
                    self.report.audit_ids["initial_patch"] = failed_job.patch_id
                    self.recorder.check(
                        "failure_injection.retry_recorded",
                        failed_job.status == "retry"
                        and failed_patch is not None
                        and failed_patch.status == "repair_required",
                        expected="retry / repair_required",
                        actual=(
                            f"{failed_job.status} / "
                            f"{failed_patch.status if failed_patch else 'missing'}"
                        ),
                        required=True,
                    )
                    self.recorder.check(
                        "failure_injection.partial_write_exists",
                        bool(partial_page) and fault.mutation_attempts == 1,
                        expected="Neo4j page present and one Qdrant failure",
                        actual={
                            "page": bool(partial_page),
                            "qdrant_attempts": fault.mutation_attempts,
                        },
                        required=True,
                    )

                with self.recorder.stage("restart_recovery") as metrics:
                    restarted = _stores(ledger)
                    recovery_worker = IndexWorker(
                        restarted.outbox,
                        publish_factory=lambda: self._publisher(restarted),
                        worker_id="business-e2e-restarted",
                        retry_base_seconds=0.0,
                        retry_max_seconds=0.0,
                        telemetry=restarted.telemetry,
                    )
                    recovered_job = recovery_worker.process_once()
                    if recovered_job is None:
                        raise RuntimeError("Restarted worker did not recover the retry job")
                    first_version = restarted.versions.get_by_patch(recovered_job.patch_id)
                    graph = GraphVectorStore()
                    try:
                        page = graph.neo4j.get_webpages_batch([self.site.canonical_url]).get(
                            self.site.canonical_url
                        )
                        blocks_before = graph.neo4j.get_blocks_for_webpage(
                            self.site.canonical_url
                        )
                        vectors_before = graph.qdrant.retrieve_vectors(
                            "blocks", [str(item["block_id"]) for item in blocks_before]
                        )
                        consistency_before = graph.check_consistency()
                    finally:
                        graph.close()
                    metrics.update(
                        {
                            "job_status": recovered_job.status,
                            "patch_status": (
                                restarted.patches.get(recovered_job.patch_id).status
                            ),
                            "version_status": first_version.status if first_version else "missing",
                            "blocks": len(blocks_before),
                            "block_vectors": len(vectors_before),
                            "consistency": consistency_before,
                        }
                    )
                    self.report.audit_ids["initial_version"] = (
                        first_version.version_id if first_version else ""
                    )
                    self.recorder.check(
                        "restart_recovery.job_succeeded",
                        recovered_job.status == "succeeded"
                        and first_version is not None
                        and first_version.status == "published",
                        expected="succeeded / published",
                        actual=(
                            f"{recovered_job.status} / "
                            f"{first_version.status if first_version else 'missing'}"
                        ),
                        required=True,
                    )
                    self.recorder.check(
                        "restart_recovery.read_after_write",
                        bool(page)
                        and len(blocks_before) >= 2
                        and len(vectors_before) == len(blocks_before)
                        and consistency_before["blocks_consistent"]
                        and consistency_before["entities_consistent"],
                        expected="page + all block vectors + consistent entity IDs",
                        actual={
                            "page": bool(page),
                            "blocks": len(blocks_before),
                            "vectors": len(vectors_before),
                            **consistency_before,
                        },
                        required=True,
                    )

                with self.recorder.stage("hot_query_reuse") as metrics:
                    origin_before_hot = self.site.request_count
                    jobs_before_hot = restarted.outbox.stats()
                    hot_fetch = FetchTrustedPageTool(
                        fetcher=mapped_fetcher,
                        store=restarted.observations,
                    )
                    hot = asyncio.run(
                        self._agent(restarted, hot_fetch).run(self._request(query))
                    )
                    origin_delta = self.site.request_count - origin_before_hot
                    metrics.update(
                        {
                            "run_id": hot.run_id,
                            "response_status": hot.response_status,
                            "pages_fetched": hot.exploration.pages_fetched,
                            "origin_request_delta": origin_delta,
                            "evidence": len(hot.evidence),
                        }
                    )
                    self.report.audit_ids["hot_agent_run"] = hot.run_id
                    self.recorder.check(
                        "hot_query.reuses_index",
                        hot.response_status == "answered"
                        and hot.exploration.pages_fetched
                        <= int(contract.get("max_reuse_pages_fetched", 0))
                        and origin_delta
                        <= int(contract.get("max_reuse_origin_requests", 0)),
                        expected="answered; no fetch; no origin request",
                        actual={
                            "status": hot.response_status,
                            "pages_fetched": hot.exploration.pages_fetched,
                            "origin_delta": origin_delta,
                        },
                        required=True,
                    )
                    self.recorder.check(
                        "hot_query.no_duplicate_job",
                        restarted.outbox.stats() == jobs_before_hot,
                        expected=jobs_before_hot,
                        actual=restarted.outbox.stats(),
                    )

                with self.recorder.stage("freshness_update") as metrics:
                    self.site.set_version(2)
                    restarted.lifecycle.refresh_now(self.site.canonical_url)
                    freshness = FreshnessWorker(
                        restarted.lifecycle,
                        restarted.outbox,
                        restarted.observations,
                        fetch_tool=hot_fetch,
                        snapshot_tool=PageSnapshotTool(),
                        stage_tool=StagePatchTool(
                            restarted.observations, restarted.patches
                        ),
                        quality_gate=PageQualityGate(),
                        quality_store=restarted.quality,
                        worker_id="business-e2e-freshness",
                    )
                    freshness_result = asyncio.run(freshness.process_once())
                    if freshness_result is None or not freshness_result.pending_job_id:
                        raise RuntimeError("Freshness worker did not enqueue the changed page")
                    update_job_id = freshness_result.pending_job_id
                    update_job = restarted.outbox.get(update_job_id)
                    if update_job is None:
                        raise RuntimeError("Freshness index job is missing from the outbox")
                    metrics.update(
                        {
                            "lifecycle_status": freshness_result.status,
                            "job_id": update_job_id,
                            "content_hash_changed": (
                                freshness_result.pending_content_hash
                                != freshness_result.content_hash
                            ),
                            "origin_requests": self.site.request_count,
                        }
                    )
                    self.report.audit_ids["update_job"] = update_job_id
                    self.report.audit_ids["update_patch"] = update_job.patch_id
                    self.recorder.check(
                        "freshness.changed_page_enqueued",
                        freshness_result.status == "indexing"
                        and freshness_result.pending_content_hash
                        != freshness_result.content_hash,
                        expected="indexing with a new content hash",
                        actual={
                            "status": freshness_result.status,
                            "old": freshness_result.content_hash,
                            "new": freshness_result.pending_content_hash,
                        },
                        required=True,
                    )

                with self.recorder.stage("incremental_publish") as metrics:
                    embedding_before_update = self.embedder.snapshot()
                    update_worker = IndexWorker(
                        restarted.outbox,
                        publish_factory=lambda: self._publisher(restarted),
                        worker_id="business-e2e-update",
                        retry_base_seconds=0.0,
                        retry_max_seconds=0.0,
                        telemetry=restarted.telemetry,
                    )
                    updated_job = update_worker.process_once()
                    if updated_job is None or updated_job.status != "succeeded":
                        raise RuntimeError(
                            f"Incremental update did not succeed: {updated_job}"
                        )
                    update_version = restarted.versions.get_by_patch(updated_job.patch_id)
                    if update_version is None:
                        raise RuntimeError("Update PageVersion is missing")
                    graph = GraphVectorStore()
                    try:
                        blocks_after = graph.neo4j.get_blocks_for_webpage(
                            self.site.canonical_url
                        )
                        vectors_after = graph.qdrant.retrieve_vectors(
                            "blocks", [str(item["block_id"]) for item in blocks_after]
                        )
                        consistency_after = graph.check_consistency()
                    finally:
                        graph.close()
                    stable_ids = sorted(
                        set(update_version.diff.unchanged_ids) & set(vectors_before)
                    )
                    stable_vectors_equal = all(
                        vectors_before[item] == vectors_after.get(item)
                        for item in stable_ids
                    )
                    fact_stats = restarted.facts.stats()
                    embedding_after_update = self.embedder.snapshot()
                    metrics.update(
                        {
                            "version_id": update_version.version_id,
                            "version_status": update_version.status,
                            "new_blocks": update_version.diff.new_count,
                            "modified_blocks": len(update_version.diff.modified_ids),
                            "unchanged_blocks": len(update_version.diff.unchanged_ids),
                            "block_embeddings": update_version.block_embeddings,
                            "stable_vectors_equal": stable_vectors_equal,
                            "fact_stats": fact_stats,
                            "consistency": consistency_after,
                            "embedding_delta": {
                                key: embedding_after_update[key]
                                - embedding_before_update[key]
                                for key in embedding_after_update
                            },
                        }
                    )
                    self.report.audit_ids["update_version"] = update_version.version_id
                    self.recorder.check(
                        "incremental_update.stable_vectors_reused",
                        len(stable_ids)
                        >= int(contract.get("min_stable_blocks_after_update", 1))
                        and stable_vectors_equal
                        and update_version.block_embeddings
                        < update_version.diff.new_count,
                        expected="stable vector unchanged and partial block embedding",
                        actual={
                            "stable_ids": len(stable_ids),
                            "equal": stable_vectors_equal,
                            "embedded": update_version.block_embeddings,
                            "new_count": update_version.diff.new_count,
                        },
                        required=True,
                    )
                    self.recorder.check(
                        "incremental_update.fact_temporality",
                        fact_stats["active"]
                        >= int(contract.get("min_active_facts", 1))
                        and fact_stats["retired"]
                        >= int(contract.get("min_retired_facts", 1)),
                        expected={
                            "active": f">={contract.get('min_active_facts', 1)}",
                            "retired": f">={contract.get('min_retired_facts', 1)}",
                        },
                        actual=fact_stats,
                        required=True,
                    )
                    self.recorder.check(
                        "incremental_update.cross_store_consistency",
                        consistency_after["blocks_consistent"]
                        and consistency_after["entities_consistent"],
                        expected=True,
                        actual=consistency_after,
                        required=True,
                    )

                with self.recorder.stage("idempotent_replay") as metrics:
                    graph = GraphVectorStore()
                    try:
                        digest_before = _inventory_digest(graph)
                    finally:
                        graph.close()
                    facts_before_replay = restarted.facts.stats()
                    replay = self._publisher(restarted).run(
                        PublishPatchInput(patch_id=self.report.audit_ids["update_patch"])
                    )
                    graph = GraphVectorStore()
                    try:
                        digest_after = _inventory_digest(graph)
                    finally:
                        graph.close()
                    facts_after_replay = restarted.facts.stats()
                    metrics.update(
                        {
                            "patch_status": replay.patch.status,
                            "inventory_digest_equal": digest_before == digest_after,
                            "fact_stats_equal": facts_before_replay == facts_after_replay,
                        }
                    )
                    self.recorder.check(
                        "idempotency.published_patch_replay",
                        replay.patch.status == "published"
                        and digest_before == digest_after
                        and facts_before_replay == facts_after_replay,
                        expected="no graph/vector/fact change",
                        actual={
                            "status": replay.patch.status,
                            "inventory_equal": digest_before == digest_after,
                            "facts_equal": facts_before_replay == facts_after_replay,
                        },
                        required=True,
                    )

                with self.recorder.stage("updated_hot_query") as metrics:
                    origin_before_updated_query = self.site.request_count
                    updated_query = (
                        f"Who approves {self.token} production database access requests?"
                    )
                    current = asyncio.run(
                        self._agent(restarted, hot_fetch).run(
                            self._request(updated_query)
                        )
                    )
                    origin_delta = self.site.request_count - origin_before_updated_query
                    metrics.update(
                        {
                            "run_id": current.run_id,
                            "response_status": current.response_status,
                            "pages_fetched": current.exploration.pages_fetched,
                            "origin_request_delta": origin_delta,
                            "answer_contains_new_fact": (
                                "Security Review Board" in current.answer
                            ),
                        }
                    )
                    self.report.audit_ids["updated_agent_run"] = current.run_id
                    self.recorder.check(
                        "updated_query.uses_new_fact_without_fetch",
                        current.response_status == "answered"
                        and "Security Review Board" in current.answer
                        and origin_delta == 0,
                        expected="new approver from indexed evidence; no fetch",
                        actual={
                            "status": current.response_status,
                            "new_fact": "Security Review Board" in current.answer,
                            "origin_delta": origin_delta,
                        },
                        required=True,
                    )

            llm_delta = _usage_delta(llm_before, snapshot_llm_usage())
            reranker_delta = _usage_delta(
                reranker_before, snapshot_reranker_usage()
            )
            external_calls = llm_delta.get("llm_calls", 0) + reranker_delta.get(
                "rerank_calls", 0
            )
            self.recorder.check(
                "determinism.external_model_calls",
                external_calls <= int(contract.get("max_external_model_calls", 0)),
                expected=contract.get("max_external_model_calls", 0),
                actual=external_calls,
                details={"llm": llm_delta, "reranker": reranker_delta},
            )
            self.report.summary = {
                "origin_requests": self.site.request_count,
                "embedding": self.embedder.snapshot(),
                "extraction_calls": self.extractor.calls,
                "llm_usage": llm_delta,
                "reranker_usage": reranker_delta,
                "ledger": ledger.name,
            }
        except BaseException as exc:
            error = exc
        finally:
            crawl_config.clear()
            crawl_config.update(original_crawl)
            self.site.close()
            self.recorder.finish(error)
            self.recorder.write(self.report_path)
        if error is not None:
            raise error
        return self.report


def run_business_e2e(
    *,
    output: str | Path,
    runtime_dir: str | Path | None = None,
) -> BusinessE2EReport:
    destination = Path(output)
    runtime = Path(runtime_dir) if runtime_dir is not None else destination.parent
    return BusinessE2EScenario(runtime.resolve(), destination.resolve()).run()
