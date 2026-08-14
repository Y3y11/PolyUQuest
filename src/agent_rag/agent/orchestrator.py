"""Budgeted single-agent loop that searches, expands, fetches, and answers."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Template

from agent_rag.agent.evidence import EvidenceAssessment, EvidenceEvaluator
from agent_rag.agent.frontier import FrontierSelector
from agent_rag.agent.query_profile import QueryProfileEnricher, build_query_profile
from agent_rag.agent.schemas import (
    AgentAction,
    AgentQueryRequest,
    AgentQueryResponse,
    ExplorationSummary,
)
from agent_rag.config import agent_config, llm_config, settings, stage_model
from agent_rag.freshness import PageLifecycleStore, page_lifecycle_store
from agent_rag.indexing.outbox import IndexOutbox, index_outbox
from agent_rag.quality import PageQualityGate, PageQualityStore, page_quality_store
from agent_rag.tools._ranking import expanded_query, frontier_score, lexical_score
from agent_rag.tools.observations import ObservationStore, observation_store
from agent_rag.tools.schemas import (
    EvidenceBlock,
    EvidenceScores,
    ExpandInput,
    FetchInput,
    FrontierSeed,
    PublishPatchInput,
    RouteDecision,
    SearchInput,
    SearchOutput,
    StagePatchInput,
    ToolTraceStep,
)

if TYPE_CHECKING:
    from agent_rag.llm.client import LLMClient
    from agent_rag.telemetry.recorder import TelemetryRecorder
    from agent_rag.tools.expand import ExpandTool
    from agent_rag.tools.fetch import FetchTrustedPageTool
    from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool
    from agent_rag.tools.search import SearchTool
    from agent_rag.tools.snapshot import PageSnapshotTool

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

_ANSWER_TEMPLATE = Template(
    (Path(__file__).parent.parent / "llm" / "prompts" / "generate_answer.j2").read_text(
        encoding="utf-8"
    )
)
_GENERATION_MODEL = stage_model("generation")
_GENERATION_MAX_TOKENS = int((llm_config.get("generation", {}) or {}).get("max_tokens", 2048))


async def _noop_emit(_event: str, _data: dict[str, Any]) -> None:
    return None


class AnswerComposer:
    def __init__(self, llm_factory: Callable[[], LLMClient] | None = None):
        self._llm_factory = llm_factory

    def compose(
        self, query: str, evidence: list[EvidenceBlock], history: list[dict[str, str]]
    ) -> str:
        if not evidence:
            return "现有知识库和本次受控探索都没有找到足够证据，暂时无法可靠回答。"
        blocks = [
            {
                "content": block.content,
                "heading_context": block.heading_context,
                "source_url": block.source_url,
            }
            for block in evidence
        ]
        prompt = _ANSWER_TEMPLATE.render(
            query=query,
            blocks=blocks,
            related_links=[],
            history=history,
        )
        from agent_rag.llm.client import LLMClient, cost_stage

        llm = self._llm_factory() if self._llm_factory is not None else LLMClient()
        try:
            kwargs: dict[str, Any] = {
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": _GENERATION_MAX_TOKENS,
                "use_cache": True,
            }
            if _GENERATION_MODEL:
                kwargs["model"] = _GENERATION_MODEL
            with cost_stage("answer"):
                return llm.chat(**kwargs)
        finally:
            llm.close()


class QueryDrivenAgent:
    def __init__(
        self,
        search_tool: SearchTool | None = None,
        expand_tool: ExpandTool | None = None,
        fetch_tool: FetchTrustedPageTool | None = None,
        stage_patch_tool: StagePatchTool | None = None,
        publish_patch_tool: PublishPatchTool | None = None,
        evaluator: EvidenceEvaluator | None = None,
        profile_enricher: QueryProfileEnricher | None = None,
        frontier_selector: FrontierSelector | None = None,
        composer: AnswerComposer | None = None,
        observations: ObservationStore = observation_store,
        snapshot_tool: PageSnapshotTool | None = None,
        indexing_outbox: IndexOutbox = index_outbox,
        quality_gate: PageQualityGate | None = None,
        quality_store: PageQualityStore = page_quality_store,
        lifecycle_store: PageLifecycleStore = page_lifecycle_store,
        telemetry: TelemetryRecorder | None = None,
    ):
        if search_tool is None:
            from agent_rag.tools.search import SearchTool

            search_tool = SearchTool()
        if expand_tool is None:
            from agent_rag.tools.expand import ExpandTool

            expand_tool = ExpandTool()
        if fetch_tool is None:
            from agent_rag.tools.fetch import FetchTrustedPageTool

            fetch_tool = FetchTrustedPageTool(store=observations)
        if stage_patch_tool is None or publish_patch_tool is None:
            from agent_rag.tools.graph_patch import PublishPatchTool, StagePatchTool

            stage_patch_tool = stage_patch_tool or StagePatchTool(observations=observations)
            publish_patch_tool = publish_patch_tool or PublishPatchTool(observations=observations)
        self.search_tool = search_tool
        self.expand_tool = expand_tool
        self.fetch_tool = fetch_tool
        self.stage_patch_tool = stage_patch_tool
        self.publish_patch_tool = publish_patch_tool
        self.evaluator = evaluator or EvidenceEvaluator()
        self.profile_enricher = profile_enricher or QueryProfileEnricher()
        self.frontier_selector = frontier_selector or FrontierSelector()
        self.composer = composer or AnswerComposer()
        self.observations = observations
        if snapshot_tool is None:
            from agent_rag.tools.snapshot import PageSnapshotTool

            snapshot_tool = PageSnapshotTool()
        self.snapshot_tool = snapshot_tool
        self.indexing_outbox = indexing_outbox
        self.quality_gate = quality_gate or PageQualityGate()
        self.quality_store = quality_store
        self.lifecycle_store = lifecycle_store
        if telemetry is None:
            from agent_rag.telemetry import telemetry_recorder

            telemetry = telemetry_recorder
        self.telemetry = telemetry

    async def run(
        self,
        request: AgentQueryRequest,
        emit: EmitCallback | None = None,
        *,
        run_id: str | None = None,
        telemetry_run_id: str | None = None,
    ) -> AgentQueryResponse:
        run_id = run_id or f"run-{uuid.uuid4().hex}"
        telemetry_run_id = telemetry_run_id or run_id
        started = time.perf_counter()
        self.telemetry.start_run(
            telemetry_run_id,
            "agent_query",
            root_run_id=run_id,
            query=request.query,
            attributes={
                "explore_web": request.explore_web,
                "persist_discoveries": request.persist_discoveries,
                "freshness": request.freshness,
            },
        )
        try:
            with self.telemetry.bind(telemetry_run_id):
                response = await self._run_impl(
                    request,
                    emit,
                    run_id=run_id,
                    telemetry_run_id=telemetry_run_id,
                )
        except asyncio.CancelledError:
            self.telemetry.finish_run(telemetry_run_id, started, status="cancelled")
            raise
        except Exception as exc:
            self.telemetry.finish_run(
                telemetry_run_id,
                started,
                status="error",
                response_status="error",
                error_category=exc.__class__.__name__,
            )
            raise
        self.telemetry.finish_run(
            telemetry_run_id,
            started,
            status="error" if response.response_status == "error" else "completed",
            response_status=response.response_status,
            route_mode=response.mode,
            stop_reason=response.exploration.stop_reason,
            evidence_count=len(response.evidence),
            pages_fetched=response.exploration.pages_fetched,
            fetch_failures=response.exploration.fetch_failures,
            indexing_jobs_queued=response.exploration.indexing_jobs_queued,
        )
        return response

    async def _run_impl(
        self,
        request: AgentQueryRequest,
        emit: EmitCallback | None = None,
        *,
        run_id: str,
        telemetry_run_id: str,
    ) -> AgentQueryResponse:
        emit = emit or _noop_emit
        started = time.perf_counter()
        actions: list[AgentAction] = []
        trace: list[ToolTraceStep] = []
        summary = ExplorationSummary()
        evidence: list[EvidenceBlock] = []
        frontier: list[FrontierSeed] = []
        visited: set[str] = set()
        sequence = 0

        async def record(
            action: str,
            status: str,
            duration_ms: int = 0,
            **details: Any,
        ) -> None:
            nonlocal sequence
            sequence += 1
            item = AgentAction(
                sequence=sequence,
                action=action,
                status=status,
                duration_ms=duration_ms,
                details=details,
            )
            actions.append(item)
            self.telemetry.record_action(telemetry_run_id, item)
            await emit("action", item.model_dump())

        await emit("run_started", {"run_id": run_id, "query": request.query})
        profile_started = time.perf_counter()
        await record("query.profile", "started")
        query_profile = build_query_profile(request.query)
        query_profile = await asyncio.to_thread(
            self.profile_enricher.enrich,
            query_profile,
        )
        retrieval_query = expanded_query(request.query, query_profile)
        await record(
            "query.profile",
            "succeeded",
            int((time.perf_counter() - profile_started) * 1000),
            source=query_profile.source,
            constraints=[
                {
                    "kind": item.kind,
                    "label": item.label,
                    "field": item.field,
                    "value": item.value,
                    "aliases": item.aliases,
                    "excludes": item.excludes,
                }
                for item in query_profile.constraints
            ],
            intents=query_profile.intents,
            required_claims=[item.model_dump() for item in query_profile.required_claims],
        )
        search_started = time.perf_counter()
        await record("polyuquest.search", "started", mode=request.mode)
        try:
            search = await asyncio.to_thread(
                self.search_tool.run,
                SearchInput(
                    query=retrieval_query,
                    query_profile=query_profile,
                    mode=request.mode,
                    top_k=8,
                    history=[turn.model_dump() for turn in request.history],
                ),
            )
        except Exception as exc:
            await record(
                "polyuquest.search",
                "failed",
                int((time.perf_counter() - search_started) * 1000),
                error=str(exc),
                fallback_to_trusted_web=bool(request.explore_web and request.budget.max_pages > 0),
            )
            if not request.explore_web or request.budget.max_pages <= 0:
                answer = "检索服务当前不可用，未能获得可验证证据。请稍后重试。"
                response = AgentQueryResponse(
                    run_id=run_id,
                    answer=answer,
                    response_status="error",
                    mode="unknown",
                    actions=actions,
                    exploration=ExplorationSummary(stop_reason="initial_search_failed"),
                    elapsed_seconds=round(time.perf_counter() - started, 3),
                )
                await emit("done", response.model_dump())
                return response
            # A read-path outage must not suppress the Agent's bounded cold
            # start. Continue with an empty observation; ExpandTool will use
            # the configured trusted institutional seeds and URL allowlist.
            search = SearchOutput(
                observation_id=f"search-fallback-{uuid.uuid4().hex}",
                route=RouteDecision(
                    mode="unknown",
                    confidence=0.0,
                    source="fallback",
                    reasoning="Initial knowledge-base search failed; using trusted web seeds.",
                ),
            )
        else:
            await record(
                "polyuquest.search",
                "succeeded",
                int((time.perf_counter() - search_started) * 1000),
                evidence=len(search.evidence),
                frontier=len(search.frontier_seeds),
                route_mode=search.route.mode,
                route_source=search.route.source,
                route_confidence=search.route.confidence,
                routing_ms=next(
                    (step.duration_ms for step in search.trace if step.step == "routing"),
                    0,
                ),
                stage_durations={step.step: step.duration_ms for step in search.trace},
            )
        evidence.extend(search.evidence)
        frontier.extend(search.frontier_seeds)
        trace.extend(search.trace)
        await emit("routing", search.route.model_dump())
        await emit("evidence", {"blocks": [item.model_dump() for item in evidence]})

        assessment = self.evaluator.assess(
            request.query,
            evidence,
            freshness=request.freshness,
            can_explore=request.explore_web and request.budget.max_pages > 0,
            query_profile=query_profile,
        )
        await emit("assessment", assessment.model_dump())
        if assessment.decision == "refresh":
            frontier.extend(
                FrontierSeed(
                    url=item.source_url,
                    title=item.source_title,
                    edge_type="REFRESH_SOURCE",
                    graph_distance=1,
                    supports_sub_goals=["goal-0"],
                    already_indexed=True,
                    last_fetched_at=item.fetched_at,
                    score=1.0,
                )
                for item in evidence
                if item.source_url
            )

        while (
            assessment.decision in {"expand", "refresh"}
            and request.explore_web
            and summary.iterations < request.budget.max_iterations
            and summary.pages_fetched < request.budget.max_pages
            and time.perf_counter() - started < request.budget.max_seconds
        ):
            summary.iterations += 1
            exploration_profile = _focus_profile(query_profile, assessment.missing_claims)
            expand_started = time.perf_counter()
            await record(
                "polyuquest.expand",
                "started",
                iteration=summary.iterations,
                missing_claims=assessment.missing_claims,
            )
            try:
                expanded = await asyncio.to_thread(
                    self.expand_tool.run,
                    ExpandInput(
                        query=request.query,
                        query_profile=exploration_profile,
                        source_block_ids=[item.block_id for item in evidence],
                        source_urls=[item.source_url for item in evidence],
                        max_candidates=20,
                    ),
                )
                frontier.extend(expanded.candidates)
                trace.extend(expanded.trace)
                await record(
                    "polyuquest.expand",
                    "succeeded",
                    int((time.perf_counter() - expand_started) * 1000),
                    candidates=len(expanded.candidates),
                )
            except Exception as exc:
                await record(
                    "polyuquest.expand",
                    "failed",
                    int((time.perf_counter() - expand_started) * 1000),
                    error=str(exc),
                )

            deduped: dict[str, FrontierSeed] = {}
            for candidate in frontier:
                if candidate.url in visited or candidate.graph_distance > request.budget.max_depth:
                    continue
                candidate.score = frontier_score(
                    request.query,
                    candidate.url,
                    " ".join((candidate.title, candidate.anchor_text)),
                    exploration_profile,
                )
                old = deduped.get(candidate.url)
                if old is None or candidate.score > old.score:
                    deduped[candidate.url] = candidate
            candidates = sorted(deduped.values(), key=lambda item: (-item.score, item.url))
            summary.frontier_candidates_seen += len(candidates)
            if not candidates:
                summary.stop_reason = "frontier_exhausted"
                break

            selection_started = time.perf_counter()
            candidate, selection_source, selection_reason = await asyncio.to_thread(
                self.frontier_selector.select,
                request.query,
                candidates,
                exploration_profile,
                assessment.missing_claims,
            )
            await record(
                "frontier.select",
                "succeeded",
                int((time.perf_counter() - selection_started) * 1000),
                source=selection_source,
                reason=selection_reason,
                selected_url=candidate.url,
                shortlist=min(len(candidates), self.frontier_selector.max_candidates),
            )
            visited.add(candidate.url)
            snapshot = None
            snapshot_started = time.perf_counter()
            try:
                snapshot = await asyncio.to_thread(self.snapshot_tool.run, candidate.url)
                if snapshot.exists:
                    await record(
                        "polyuquest.load_page_snapshot",
                        "succeeded",
                        int((time.perf_counter() - snapshot_started) * 1000),
                        url=candidate.url,
                        blocks=len(snapshot.blocks),
                        has_validator=bool(
                            snapshot.page.get("etag") or snapshot.page.get("last_modified")
                        ),
                    )
            except Exception as exc:
                await record(
                    "polyuquest.load_page_snapshot",
                    "failed",
                    int((time.perf_counter() - snapshot_started) * 1000),
                    url=candidate.url,
                    error=str(exc),
                    fallback="unconditional_fetch",
                )
                snapshot = None
            fetch_started = time.perf_counter()
            await record(
                "web.fetch_trusted_page",
                "started",
                url=candidate.url,
                title=candidate.title,
                edge_type=candidate.edge_type,
                parent_url=candidate.parent_url,
                candidate_score=round(candidate.score, 4),
                depth=candidate.graph_distance,
            )
            try:
                fetch_cfg = agent_config.get("fetch", {})
                fetched = await self.fetch_tool.run(
                    FetchInput(
                        url=candidate.url,
                        query=request.query,
                        query_profile=exploration_profile,
                        run_id=run_id,
                        timeout_seconds=float(fetch_cfg.get("timeout_seconds", 15)),
                        max_bytes=int(fetch_cfg.get("max_bytes", 5_000_000)),
                        if_none_match=(
                            snapshot.page.get("etag")
                            if snapshot is not None and snapshot.blocks
                            else None
                        ),
                        if_modified_since=(
                            snapshot.page.get("last_modified")
                            if snapshot is not None and snapshot.blocks
                            else None
                        ),
                    )
                )
                summary.pages_fetched += 1
                if fetched.not_modified:
                    summary.pages_revalidated += 1
                    summary.conditional_cache_hits += 1
                    try:
                        lifecycle = await asyncio.to_thread(
                            self.lifecycle_store.mark_query_validated_unchanged,
                            fetched.metadata.final_url,
                        )
                        snapshot.page["last_validated_at"] = lifecycle.last_validated_at
                    except KeyError:
                        pass
                trace.extend(fetched.trace)
                await record(
                    "web.fetch_trusted_page",
                    "succeeded",
                    int((time.perf_counter() - fetch_started) * 1000),
                    url=fetched.metadata.final_url,
                    relevant_blocks=fetched.evidence_gain.relevant_blocks,
                    not_modified=fetched.not_modified,
                    reused_blocks=(
                        len(snapshot.blocks) if fetched.not_modified and snapshot is not None else 0
                    ),
                )
            except Exception as exc:
                summary.fetch_failures += 1
                await record(
                    "web.fetch_trusted_page",
                    "failed",
                    int((time.perf_counter() - fetch_started) * 1000),
                    url=candidate.url,
                    error=str(exc),
                )
                assessment = EvidenceAssessment(
                    decision="expand",
                    reasons=["The selected frontier page could not be fetched."],
                )
                continue

            record_data = self.observations.get(fetched.observation_id)
            quality_decision = None
            if record_data is not None and not fetched.not_modified:
                quality_started = time.perf_counter()
                await record(
                    "polyuquest.evaluate_page_quality",
                    "started",
                    observation_id=fetched.observation_id,
                )
                quality_error = None
                try:
                    quality_decision = self.quality_gate.evaluate(record_data, fetched)
                    quality_decision = await asyncio.to_thread(
                        self.quality_store.put, quality_decision
                    )
                    record_data.metadata.update(
                        {
                            "quality_action": quality_decision.action,
                            "quality_score": quality_decision.score,
                            "quality_policy_version": quality_decision.policy_version,
                            "quality_decision_id": quality_decision.decision_id,
                        }
                    )
                    await asyncio.to_thread(self.observations.put, record_data)
                except Exception as exc:
                    quality_error = exc
                    quality_decision = self.quality_gate.fallback(record_data, exc)
                    with suppress(Exception):
                        quality_decision = await asyncio.to_thread(
                            self.quality_store.put, quality_decision
                        )
                if quality_decision.action == "index":
                    summary.pages_index_accepted += 1
                elif quality_decision.action == "evidence_only":
                    summary.pages_evidence_only += 1
                else:
                    summary.pages_discarded += 1
                if quality_error is not None:
                    await record(
                        "polyuquest.evaluate_page_quality",
                        "failed",
                        int((time.perf_counter() - quality_started) * 1000),
                        error=str(quality_error),
                        fallback_action="evidence_only",
                        evidence_usable=True,
                    )
                else:
                    await record(
                        "polyuquest.evaluate_page_quality",
                        "succeeded",
                        int((time.perf_counter() - quality_started) * 1000),
                        decision_id=quality_decision.decision_id,
                        decision_action=quality_decision.action,
                        evidence_usable=quality_decision.evidence_usable,
                        score=quality_decision.score,
                        policy_version=quality_decision.policy_version,
                        reasons=quality_decision.reasons,
                        features=quality_decision.features.model_dump(),
                    )
            fetched_evidence: list[EvidenceBlock] = []
            if fetched.not_modified and snapshot is not None:
                scored_snapshot = sorted(
                    snapshot.blocks,
                    key=lambda block: (
                        -lexical_score(
                            request.query,
                            (f"{block.get('heading_context', '')} {block.get('content', '')}"),
                        )
                    ),
                )[:8]
                for block in scored_snapshot:
                    score = lexical_score(
                        request.query,
                        f"{block.get('heading_context', '')} {block.get('content', '')}",
                    )
                    fetched_evidence.append(
                        EvidenceBlock(
                            block_id=block.get("block_id", ""),
                            content=block.get("content", ""),
                            heading_context=block.get("heading_context", ""),
                            source_url=str(snapshot.page.get("url") or candidate.url),
                            source_title=str(snapshot.page.get("title", "")),
                            page_type=str(snapshot.page.get("page_type", "other")),
                            fetched_at=(
                                snapshot.page.get("last_validated_at")
                                or snapshot.page.get("fetched_at")
                                or snapshot.page.get("last_crawled")
                            ),
                            content_hash=snapshot.page.get("content_hash") or None,
                            scores=EvidenceScores(retrieval=score),
                            supports_sub_goals=["goal-0"],
                            temporary=False,
                        )
                    )
            elif (
                record_data
                and fetched.evidence_gain.relevant_blocks > 0
                and (quality_decision is None or quality_decision.evidence_usable)
            ):
                selected_ids = set(fetched.block_refs)
                for block in record_data.blocks:
                    if block.get("block_id") not in selected_ids:
                        continue
                    score = lexical_score(
                        request.query,
                        f"{block.get('heading_context', '')} {block.get('content', '')}",
                    )
                    fetched_evidence.append(
                        EvidenceBlock(
                            block_id=block.get("block_id", ""),
                            content=block.get("content", ""),
                            heading_context=block.get("heading_context", ""),
                            source_url=fetched.metadata.final_url,
                            source_title=fetched.metadata.title,
                            page_type=fetched.metadata.page_type,
                            fetched_at=fetched.metadata.fetched_at,
                            content_hash=fetched.metadata.content_hash,
                            scores=EvidenceScores(retrieval=score),
                            supports_sub_goals=["goal-0"],
                            observation_id=fetched.observation_id,
                            temporary=True,
                        )
                    )
            evidence = _merge_evidence(evidence, fetched_evidence)
            if not fetched.not_modified:
                summary.temporary_evidence_blocks += len(fetched_evidence)
            for item in fetched.discovered_links:
                item.graph_distance = candidate.graph_distance + 1
                frontier.append(item)
            await emit("evidence", {"blocks": [item.model_dump() for item in evidence]})

            if (
                request.persist_discoveries
                and record_data is not None
                and quality_decision is not None
                and quality_decision.action == "index"
            ):
                publish_started = time.perf_counter()
                action_name = (
                    "polyuquest.queue_index_patch"
                    if settings.agent_async_indexing
                    else "polyuquest.publish_patch"
                )
                await record(
                    action_name,
                    "started",
                    observation_id=fetched.observation_id,
                )
                try:
                    if settings.agent_async_indexing:
                        existing_job = await asyncio.to_thread(
                            self.indexing_outbox.get_by_snapshot,
                            str(record_data.metadata.get("url", "")),
                            str(record_data.metadata.get("content_hash", "")),
                        )
                        if existing_job is None:
                            patch = await asyncio.to_thread(
                                self.stage_patch_tool.run,
                                StagePatchInput(
                                    observation_id=fetched.observation_id,
                                    run_id=run_id,
                                ),
                            )
                            job, created = await asyncio.to_thread(
                                self.indexing_outbox.enqueue, patch
                            )
                            if not created and job.patch_id != patch.patch_id:
                                await asyncio.to_thread(
                                    self.stage_patch_tool.discard_duplicate,
                                    patch.patch_id,
                                )
                        else:
                            job, created = existing_job, False
                        summary.indexing_jobs_queued += int(created)
                        await record(
                            action_name,
                            "succeeded",
                            int((time.perf_counter() - publish_started) * 1000),
                            patch_id=job.patch_id,
                            job_id=job.job_id,
                            job_status=job.status,
                            deduplicated=not created,
                            content_hash=job.content_hash,
                        )
                    else:
                        patch = await asyncio.to_thread(
                            self.stage_patch_tool.run,
                            StagePatchInput(
                                observation_id=fetched.observation_id,
                                run_id=run_id,
                            ),
                        )
                        published = await asyncio.to_thread(
                            self.publish_patch_tool.run,
                            PublishPatchInput(patch_id=patch.patch_id),
                        )
                        if published.patch.status == "published":
                            summary.patches_published += 1
                            for item in evidence:
                                if item.observation_id == fetched.observation_id:
                                    item.temporary = False
                        await record(
                            action_name,
                            "succeeded" if published.read_after_write_ok else "failed",
                            int((time.perf_counter() - publish_started) * 1000),
                            patch_id=patch.patch_id,
                            patch_status=published.patch.status,
                            operation=published.patch.operation,
                            previous_content_hash=published.patch.previous_content_hash,
                            content_hash=published.patch.content_hash,
                            webpages_written=published.webpages_written,
                            blocks_written=published.blocks_written,
                            links_written=published.links_written,
                            blocks_deleted=published.blocks_deleted,
                            links_deleted=published.links_deleted,
                            read_after_write_ok=published.read_after_write_ok,
                        )
                except Exception as exc:
                    await record(
                        action_name,
                        "failed",
                        int((time.perf_counter() - publish_started) * 1000),
                        error=str(exc),
                    )

            assessment = self.evaluator.assess(
                request.query,
                evidence,
                freshness=request.freshness,
                can_explore=True,
                query_profile=query_profile,
            )
            await emit("assessment", assessment.model_dump())

        if not summary.stop_reason:
            if assessment.decision == "answer":
                summary.stop_reason = "evidence_sufficient"
            elif time.perf_counter() - started >= request.budget.max_seconds:
                summary.stop_reason = "time_budget_exhausted"
            elif summary.iterations >= request.budget.max_iterations:
                summary.stop_reason = "iteration_budget_exhausted"
            elif summary.pages_fetched >= request.budget.max_pages:
                summary.stop_reason = "page_budget_exhausted"
            else:
                summary.stop_reason = "exploration_disabled"

        answer_started = time.perf_counter()
        compose_failed = False
        answer_evidence = [] if assessment.decision == "abstain" else evidence
        await record("answer.compose", "started", evidence=len(answer_evidence))
        try:
            answer = await asyncio.to_thread(
                self.composer.compose,
                request.query,
                answer_evidence,
                [turn.model_dump() for turn in request.history],
            )
            await record(
                "answer.compose",
                "succeeded",
                int((time.perf_counter() - answer_started) * 1000),
            )
        except Exception as exc:
            compose_failed = True
            await record(
                "answer.compose",
                "failed",
                int((time.perf_counter() - answer_started) * 1000),
                error=str(exc),
            )
            answer = "已找到部分证据，但答案生成服务当前不可用。请稍后重试。"

        if assessment.decision == "abstain" or not evidence:
            response_status = "abstained"
        elif compose_failed:
            response_status = "partial"
        elif assessment.decision == "answer":
            response_status = "answered"
        else:
            response_status = "partial"
        response = AgentQueryResponse(
            run_id=run_id,
            answer=answer,
            response_status=response_status,
            mode=search.route.mode,
            evidence=evidence,
            actions=actions,
            exploration=summary,
            pipeline_trace=trace,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        await emit("done", response.model_dump())
        return response


def _merge_evidence(
    current: list[EvidenceBlock], incoming: list[EvidenceBlock], limit: int = 16
) -> list[EvidenceBlock]:
    by_id = {item.block_id: item for item in current}
    for item in incoming:
        old = by_id.get(item.block_id)
        if old is None or item.scores.retrieval > old.scores.retrieval:
            by_id[item.block_id] = item
    return sorted(
        by_id.values(),
        key=lambda item: (-(item.scores.reranker or item.scores.retrieval), item.block_id),
    )[:limit]


def _focus_profile(query_profile, missing_claims: list[str]):
    """Narrow navigation scoring to claims the evidence has not supported yet."""
    if not missing_claims:
        return query_profile
    wanted = set(missing_claims)
    focused = [item for item in query_profile.required_claims if item.claim in wanted]
    return query_profile.model_copy(update={"required_claims": focused})
