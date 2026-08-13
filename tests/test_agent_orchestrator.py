from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_rag.agent.orchestrator import QueryDrivenAgent
from agent_rag.agent.schemas import AgentBudget, AgentQueryRequest
from agent_rag.tools.observations import ObservationRecord, ObservationStore
from agent_rag.tools.schemas import (
    EvidenceBlock,
    EvidenceGain,
    EvidenceScores,
    ExpandOutput,
    FetchMetadata,
    FetchOutput,
    FrontierSeed,
    GraphPatch,
    PublishPatchOutput,
    RouteDecision,
    SearchOutput,
)
from agent_rag.tools.snapshot import PageSnapshot


class FakeSearch:
    def __init__(self, evidence: list[EvidenceBlock]):
        self.evidence = evidence
        self.calls = 0

    def run(self, _tool_input):
        self.calls += 1
        return SearchOutput(
            observation_id="search-1",
            route=RouteDecision(mode="A", confidence=0.9),
            evidence=self.evidence,
        )


class FailingSearch:
    def run(self, _tool_input):
        raise ConnectionError("knowledge stores unavailable")


class FakeExpand:
    def __init__(self):
        self.calls = 0

    def run(self, _tool_input):
        self.calls += 1
        return ExpandOutput(
            candidates=[
                FrontierSeed(
                    url="https://www.polyu.edu.hk/study/",
                    title="Study",
                    score=0.8,
                )
            ]
        )


class FakeFetch:
    def __init__(
        self, store: ObservationStore, fail: bool = False, not_modified: bool = False
    ):
        self.store = store
        self.fail = fail
        self.calls = 0
        self.not_modified = not_modified
        self.last_input = None

    async def run(self, tool_input):
        self.calls += 1
        self.last_input = tool_input
        if self.fail:
            raise RuntimeError("fetch failed")
        now = datetime.now(UTC).isoformat()
        if self.not_modified:
            return FetchOutput(
                observation_id="fetch-304",
                metadata=FetchMetadata(
                    requested_url=str(tool_input.url),
                    final_url=str(tool_input.url),
                    fetched_at=now,
                    content_hash="",
                    etag="etag-1",
                    last_modified="Wed, 12 Aug 2026 00:00:00 GMT",
                    status_code=304,
                ),
                not_modified=True,
            )
        block = {
            "block_id": "fresh-block",
            "content": "The admission deadline is 30 November.",
            "heading_context": "Admission",
            "url": str(tool_input.url),
        }
        self.store.put(
            ObservationRecord(
                observation_id="fetch-1",
                run_id=tool_input.run_id,
                raw_html="<p>deadline</p>",
                metadata={
                    "url": str(tool_input.url),
                    "title": "Admission",
                    "page_type": "programme",
                    "fetched_at": now,
                    "content_hash": "hash",
                },
                blocks=[block],
            )
        )
        return FetchOutput(
            observation_id="fetch-1",
            metadata=FetchMetadata(
                requested_url=str(tool_input.url),
                final_url=str(tool_input.url),
                title="Admission",
                page_type="programme",
                fetched_at=now,
                content_hash="hash",
                status_code=200,
            ),
            block_refs=["fresh-block"],
            evidence_gain=EvidenceGain(
                relevant_blocks=1, total_blocks=1, lexical_coverage=0.8
            ),
        )


class FakeStage:
    def __init__(self):
        self.calls = 0

    def run(self, tool_input):
        self.calls += 1
        now = datetime.now(UTC).isoformat()
        return GraphPatch(
            patch_id="patch-1",
            observation_id=tool_input.observation_id,
            run_id=tool_input.run_id,
            source_url="https://www.polyu.edu.hk/study/",
            content_hash="hash",
            created_at=now,
            updated_at=now,
        )

    def discard_duplicate(self, _patch_id):
        return True


class FakePublish:
    def __init__(self):
        self.calls = 0

    def run(self, tool_input):
        self.calls += 1
        now = datetime.now(UTC).isoformat()
        patch = GraphPatch(
            patch_id=tool_input.patch_id,
            observation_id="fetch-1",
            run_id="run",
            source_url="https://www.polyu.edu.hk/study/",
            content_hash="hash",
            status="published",
            created_at=now,
            updated_at=now,
        )
        return PublishPatchOutput(patch=patch, read_after_write_ok=True)


class FakeOutbox:
    def __init__(self, existing_job=None):
        self.calls = 0
        self.existing_job = existing_job

    def enqueue(self, patch):
        self.calls += 1
        return (
            type(
                "Job",
                (),
                {
                    "job_id": "job-1",
                    "status": "pending",
                },
            )(),
            True,
        )

    def get_by_snapshot(self, _source_url, _content_hash):
        return self.existing_job


class RacingOutbox(FakeOutbox):
    def enqueue(self, _patch):
        self.calls += 1
        return (
            type(
                "Job",
                (),
                {
                    "job_id": "job-winner",
                    "patch_id": "patch-winner",
                    "status": "pending",
                    "content_hash": "hash",
                },
            )(),
            False,
        )


class FakeComposer:
    def compose(self, _query, evidence, _history):
        return f"answer from {len(evidence)} evidence blocks"


class FailingComposer:
    def compose(self, _query, _evidence, _history):
        raise ConnectionError("generation unavailable")


class FakeProfileEnricher:
    def enrich(self, profile):
        return profile


class FakeSnapshot:
    def __init__(self, exists: bool = False):
        self.exists = exists

    def run(self, url):
        if not self.exists:
            return PageSnapshot()
        return PageSnapshot(
            page={
                "url": url,
                "title": "Admission",
                "page_type": "programme",
                "fetched_at": "2026-08-12T00:00:00+00:00",
                "content_hash": "hash",
                "etag": "etag-1",
                "last_modified": "Wed, 12 Aug 2026 00:00:00 GMT",
            },
            blocks=[
                {
                    "block_id": "cached-block",
                    "content": "The admission deadline is 30 November.",
                    "heading_context": "Admission",
                }
            ],
        )


def _existing_evidence() -> EvidenceBlock:
    return EvidenceBlock(
        block_id="existing",
        content="The admission deadline is 30 November.",
        source_url="https://www.polyu.edu.hk/study/",
        fetched_at=datetime.now(UTC).isoformat(),
        scores=EvidenceScores(retrieval=0.9),
    )


class QueryDrivenAgentTests(unittest.IsolatedAsyncioTestCase):
    def build_agent(
        self, *, evidence=None, fetch_fail=False, not_modified=False, snapshot=False
    ):
        store = ObservationStore()
        search = FakeSearch(evidence or [])
        expand = FakeExpand()
        fetch = FakeFetch(store, fail=fetch_fail, not_modified=not_modified)
        stage = FakeStage()
        publish = FakePublish()
        outbox = FakeOutbox()
        agent = QueryDrivenAgent(
            search_tool=search,
            expand_tool=expand,
            fetch_tool=fetch,
            stage_patch_tool=stage,
            publish_patch_tool=publish,
            profile_enricher=FakeProfileEnricher(),
            composer=FakeComposer(),
            observations=store,
            snapshot_tool=FakeSnapshot(exists=snapshot),
            indexing_outbox=outbox,
        )
        return agent, search, expand, fetch, stage, publish, outbox

    async def test_sufficient_existing_evidence_does_not_fetch(self) -> None:
        agent, _, expand, fetch, stage, _, _ = self.build_agent(
            evidence=[_existing_evidence()]
        )
        result = await agent.run(
            AgentQueryRequest(query="admission deadline", persist_discoveries=False)
        )
        self.assertEqual(result.response_status, "answered")
        self.assertEqual(fetch.calls, 0)
        self.assertEqual(expand.calls, 0)
        self.assertEqual(stage.calls, 0)

    async def test_miss_expands_and_uses_temporary_evidence(self) -> None:
        agent, _, expand, fetch, stage, _, _ = self.build_agent()
        result = await agent.run(
            AgentQueryRequest(query="admission deadline", persist_discoveries=False)
        )
        self.assertEqual(result.response_status, "answered")
        self.assertEqual(expand.calls, 1)
        self.assertEqual(fetch.calls, 1)
        self.assertTrue(any(item.temporary for item in result.evidence))
        self.assertEqual(stage.calls, 0)

    async def test_search_outage_falls_back_to_bounded_trusted_web(self) -> None:
        agent, _, expand, fetch, _, _, _ = self.build_agent()
        agent.search_tool = FailingSearch()
        result = await agent.run(
            AgentQueryRequest(query="admission deadline", persist_discoveries=False)
        )
        self.assertEqual(result.response_status, "answered")
        self.assertEqual(expand.calls, 1)
        self.assertEqual(fetch.calls, 1)
        failed_search = next(
            action
            for action in result.actions
            if action.action == "polyuquest.search" and action.status == "failed"
        )
        self.assertTrue(failed_search.details["fallback_to_trusted_web"])

    async def test_stream_emits_auditable_agent_decisions(self) -> None:
        agent, _, _, _, _, _, _ = self.build_agent()
        events: list[tuple[str, dict]] = []

        async def emit(event: str, data: dict) -> None:
            events.append((event, data))

        await agent.run(
            AgentQueryRequest(query="admission deadline", persist_discoveries=False),
            emit=emit,
        )

        event_names = [event for event, _ in events]
        self.assertIn("routing", event_names)
        self.assertIn("assessment", event_names)
        self.assertEqual(event_names[-1], "done")
        started_fetch = next(
            data
            for event, data in events
            if event == "action"
            and data["action"] == "web.fetch_trusted_page"
            and data["status"] == "started"
        )
        self.assertEqual(started_fetch["details"]["edge_type"], "LINKS_TO")
        self.assertIn("candidate_score", started_fetch["details"])

    async def test_default_exploration_publishes_patch(self) -> None:
        agent, _, _, _, stage, publish, outbox = self.build_agent()
        result = await agent.run(AgentQueryRequest(query="admission deadline"))
        self.assertEqual(result.exploration.patches_published, 0)
        self.assertEqual(result.exploration.indexing_jobs_queued, 1)
        self.assertEqual(stage.calls, 1)
        self.assertEqual(publish.calls, 0)
        self.assertEqual(outbox.calls, 1)
        self.assertTrue(all(item.temporary for item in result.evidence))

    async def test_fetch_failure_is_bounded_and_abstains(self) -> None:
        agent, _, _, fetch, _, _, _ = self.build_agent(fetch_fail=True)
        result = await agent.run(
            AgentQueryRequest(
                query="unknown question",
                persist_discoveries=False,
                budget=AgentBudget(max_iterations=2, max_pages=2, max_depth=1),
            )
        )
        self.assertEqual(result.response_status, "abstained")
        self.assertLessEqual(fetch.calls, 2)
        self.assertIn(
            result.exploration.stop_reason,
            {"frontier_exhausted", "iteration_budget_exhausted"},
        )

    async def test_not_modified_reuses_snapshot_without_publishing(self) -> None:
        agent, _, _, fetch, stage, publish, outbox = self.build_agent(
            not_modified=True, snapshot=True
        )
        result = await agent.run(AgentQueryRequest(query="admission deadline"))
        self.assertEqual(fetch.last_input.if_none_match, "etag-1")
        self.assertEqual(
            fetch.last_input.if_modified_since,
            "Wed, 12 Aug 2026 00:00:00 GMT",
        )
        self.assertEqual(result.exploration.conditional_cache_hits, 1)
        self.assertTrue(any(item.block_id == "cached-block" for item in result.evidence))
        self.assertTrue(all(not item.temporary for item in result.evidence))
        self.assertEqual(stage.calls, 0)
        self.assertEqual(publish.calls, 0)
        self.assertEqual(outbox.calls, 0)

    async def test_irrelevant_evidence_without_exploration_abstains(self) -> None:
        irrelevant = EvidenceBlock(
            block_id="irrelevant",
            content="Campus catering opening hours.",
            source_url="https://www.polyu.edu.hk/campus/",
            scores=EvidenceScores(retrieval=0.01),
        )
        agent, _, _, fetch, _, _, _ = self.build_agent(evidence=[irrelevant])
        result = await agent.run(
            AgentQueryRequest(query="research scholarship", explore_web=False)
        )
        self.assertEqual(result.response_status, "abstained")
        self.assertEqual(fetch.calls, 0)

    async def test_generation_failure_returns_partial_not_answered(self) -> None:
        agent, _, _, _, _, _, _ = self.build_agent(evidence=[_existing_evidence()])
        agent.composer = FailingComposer()
        result = await agent.run(
            AgentQueryRequest(query="admission deadline", persist_discoveries=False)
        )
        self.assertEqual(result.response_status, "partial")
        self.assertIn("答案生成服务当前不可用", result.answer)

    async def test_duplicate_snapshot_does_not_stage_an_orphan_patch(self) -> None:
        agent, _, _, _, stage, publish, _ = self.build_agent()
        agent.indexing_outbox = FakeOutbox(
            existing_job=type(
                "Job",
                (),
                {
                    "job_id": "job-existing",
                    "patch_id": "patch-existing",
                    "status": "running",
                    "content_hash": "hash",
                },
            )()
        )
        result = await agent.run(AgentQueryRequest(query="admission deadline"))
        self.assertEqual(stage.calls, 0)
        self.assertEqual(publish.calls, 0)
        self.assertEqual(result.exploration.indexing_jobs_queued, 0)
        queued = next(
            item
            for item in result.actions
            if item.action == "polyuquest.queue_index_patch"
            and item.status == "succeeded"
        )
        self.assertTrue(queued.details["deduplicated"])

    async def test_enqueue_race_discards_losing_staged_patch(self) -> None:
        agent, _, _, _, stage, publish, _ = self.build_agent()
        discarded: list[str] = []
        stage.discard_duplicate = discarded.append
        agent.indexing_outbox = RacingOutbox()
        result = await agent.run(AgentQueryRequest(query="admission deadline"))
        self.assertEqual(stage.calls, 1)
        self.assertEqual(publish.calls, 0)
        self.assertEqual(discarded, ["patch-1"])
        self.assertEqual(result.exploration.indexing_jobs_queued, 0)


if __name__ == "__main__":
    unittest.main()
