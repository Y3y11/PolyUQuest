from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from agent_rag.config import crawl_config
from agent_rag.e2e.contract import (
    BusinessE2EReport,
    ContractRecorder,
    compare_reused_block_vectors,
)
from agent_rag.e2e.deterministic import (
    DeterministicEmbedder,
    DeterministicKnowledgeExtractor,
)
from agent_rag.e2e.site import MappedFixtureFetcher, VersionedFixtureSite, fixture_html
from agent_rag.quality import PageQualityGate
from agent_rag.tools.fetch import FetchTrustedPageTool
from agent_rag.tools.observations import ObservationStore
from agent_rag.tools.schemas import FetchInput

ROOT = Path(__file__).resolve().parents[1]


def test_deterministic_embedding_is_stable_normalized_and_shared() -> None:
    embedder = DeterministicEmbedder(64)
    text = "E2E-123 production database access approval"
    first = embedder.embed_many([text])[0]
    second = embedder.embed_query(text)
    different = embedder.embed_query("unrelated cafeteria menu")

    assert first == second
    assert first != different
    assert len(first) == 64
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6
    assert embedder.snapshot() == {
        "batch_calls": 1,
        "query_calls": 2,
        "texts_embedded": 1,
    }


def test_fixture_keeps_stable_dom_and_changes_approval_fact() -> None:
    v1 = fixture_html("e2e-contract", 1)
    v2 = fixture_html("e2e-contract", 2)
    assert 'section id="operations"' in v1
    assert 'section id="operations"' in v2
    assert "Platform Team" in v1
    assert "Security Review Board" in v2
    assert v1 != v2


def test_fixture_fetch_uses_real_http_etag_and_structure() -> None:
    original = dict(crawl_config)
    site = VersionedFixtureSite(
        "e2e-fetch",
        "e2e.test",
        "/access/e2e-fetch/",
    )
    try:
        crawl_config.update(
            {
                "domain_whitelist": ["e2e.test"],
                "seed_urls": [site.canonical_url],
            }
        )
        with site:
            store = ObservationStore()
            tool = FetchTrustedPageTool(
                fetcher=MappedFixtureFetcher(site),
                store=store,
            )
            first = asyncio.run(
                tool.run(
                    FetchInput(
                        url=site.canonical_url,
                        query="production database access steps approval",
                        run_id="run-fetch",
                    )
                )
            )
            record = store.get(first.observation_id)
            assert record is not None
            assert len(record.blocks) >= 2
            assert first.evidence_gain.relevant_blocks >= 1
            assert first.metadata.etag
            quality = PageQualityGate().evaluate(record, first)
            assert quality.action == "index"

            unchanged = asyncio.run(
                tool.run(
                    FetchInput(
                        url=site.canonical_url,
                        query="production database access steps approval",
                        run_id="run-fetch-304",
                        if_none_match=first.metadata.etag,
                    )
                )
            )
            assert unchanged.not_modified
            assert unchanged.metadata.status_code == 304
            assert site.request_count == 2
    finally:
        site.close()
        crawl_config.clear()
        crawl_config.update(original)


def test_deterministic_extractor_moves_fact_to_new_approver() -> None:
    extractor = DeterministicKnowledgeExtractor("e2e-fact")
    base = {
        "block_id": "procedure",
        "content": "Approval is required from Platform Team.",
    }
    old = extractor("http://e2e.test/access/", [base])
    new = extractor(
        "http://e2e.test/access/",
        [
            {
                **base,
                "content": "Approval is required from Security Review Board.",
            }
        ],
    )
    assert old.relations[0].target == "Platform Team e2e-fact"
    assert new.relations[0].target == "Security Review Board e2e-fact"
    assert old.relations[0].source_block_refs == ["procedure"]
    assert extractor.calls == 2


def test_report_is_atomic_machine_readable_and_redacts_secrets(tmp_path: Path) -> None:
    report = BusinessE2EReport(
        scenario_id="fixture",
        scenario_token="token",
    )
    recorder = ContractRecorder(report)
    recorder.check("example", True, expected=1, actual=1)
    recorder.finish(RuntimeError("Bearer secret-token-value failed"))
    path = recorder.write(tmp_path / "report.json")
    loaded = BusinessE2EReport.model_validate_json(path.read_text(encoding="utf-8"))

    assert loaded.status == "failed"
    assert "secret-token-value" not in loaded.error
    assert "[REDACTED]" in loaded.error
    assert not path.with_suffix(".json.tmp").exists()


def test_vector_reuse_contract_includes_relocated_blocks() -> None:
    evidence = compare_reused_block_vectors(
        unchanged_ids=["stable"],
        relocated_pairs=[("old-location", "new-location")],
        vectors_before={
            "stable": [0.1, 0.2],
            "old-location": [0.3, 0.4],
        },
        vectors_after={
            "stable": [0.1, 0.2],
            "new-location": [0.3, 0.4],
        },
    )

    assert evidence == {
        "same_id_count": 1,
        "relocated_count": 1,
        "candidate_count": 2,
        "equal": True,
    }


def test_config_and_workflow_encode_real_store_gate() -> None:
    config = yaml.safe_load(
        (ROOT / "configs" / "business_e2e.yaml").read_text(encoding="utf-8")
    )
    workflow = (
        ROOT / ".github" / "workflows" / "business-e2e-gate.yml"
    ).read_text(encoding="utf-8")
    compose = (ROOT / "compose.e2e.yml").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert config["schema_version"] == 1
    assert config["contract"]["max_external_model_calls"] == 0
    assert "neo4j:5.26.28" in workflow
    assert "qdrant/qdrant:v1.18.2" in workflow
    assert "RERANKER_MODE: first_stage_only" in workflow
    assert "agent-rag-business-e2e" in workflow
    assert "if: always()" in workflow
    assert "persist-credentials: false" in workflow
    assert "127.0.0.1:17687:7687" in compose
    assert "127.0.0.1:16333:6333" in compose
    assert "agent-rag-business-e2e =" in pyproject
