from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agent_rag.config import Settings
from agent_rag.deployment import runtime_profile
from agent_rag.e2e.fixture_app import app, state
from agent_rag.e2e.topology_driver import TopologyE2EDriver

ROOT = Path(__file__).resolve().parents[1]


def test_topology_mode_is_valid_only_in_test_environment() -> None:
    with patch.object(runtime_profile, "_module_available", return_value=False):
        selected = Settings(
            _env_file=None,
            app_environment="test",
            app_runtime_profile="remote",
            embedding_provider="siliconflow",
            business_e2e_mode="topology",
            business_e2e_token="topology-contract",
            business_e2e_fixture_origin="http://fixture:8080",
        )
    assert selected.business_e2e_mode == "topology"

    with (
        patch.object(runtime_profile, "_module_available", return_value=False),
        pytest.raises(ValueError, match="only allowed"),
    ):
        Settings(
            _env_file=None,
            app_environment="production",
            app_runtime_profile="remote",
            embedding_provider="siliconflow",
            business_e2e_mode="topology",
            business_e2e_token="topology-contract",
            business_e2e_fixture_origin="http://fixture:8080",
        )


def test_claim_delay_requires_topology_mode() -> None:
    with (
        patch.object(runtime_profile, "_module_available", return_value=False),
        pytest.raises(ValueError, match="requires BUSINESS_E2E_MODE"),
    ):
        Settings(
            _env_file=None,
            app_environment="test",
            app_runtime_profile="remote",
            embedding_provider="siliconflow",
            business_e2e_claim_delay_seconds=1,
        )


def test_fixture_service_supports_etag_and_version_control() -> None:
    state.set_version(1)
    before = state.read().requests
    with TestClient(app) as client:
        first = client.get(f"/access/{state.token}/")
        assert first.status_code == 200
        assert "Platform Team" in first.text
        etag = first.headers["etag"]

        unchanged = client.get(
            f"/access/{state.token}/",
            headers={"If-None-Match": etag},
        )
        assert unchanged.status_code == 304

        changed = client.post("/__control/version/2")
        assert changed.status_code == 200
        second = client.get(f"/access/{state.token}/")
        assert second.status_code == 200
        assert "Security Review Board" in second.text
        assert state.read().requests - before == 3


def test_topology_driver_parses_audited_sse_contract(tmp_path: Path) -> None:
    driver = TopologyE2EDriver(
        compose_file=ROOT / "compose.topology-e2e.yml",
        project="polyuquest-contract",
        token="topology-contract",
        api_base="http://api.test",
        fixture_base="http://fixture.test",
        output=tmp_path / "report.json",
        timeout_seconds=1,
        build=False,
    )
    driver.client.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "topology-contract-reader"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Request-ID": "req-contract",
            },
            text=(
                'event: run_started\ndata: {"run_id":"run-contract"}\n\n'
                'event: action\ndata: {"action":"query.profile"}\n\n'
                'event: done\ndata: {"response_status":"answered"}\n\n'
            ),
        )

    driver.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        events, headers = driver._sse_query()
    finally:
        driver.client.close()

    assert [event for event, _ in events] == ["run_started", "action", "done"]
    assert driver._done(events)["response_status"] == "answered"
    assert headers == {
        "content-type": "text/event-stream",
        "x-request-id": "req-contract",
    }


def test_compose_encodes_split_process_and_claim_takeover() -> None:
    raw = (ROOT / "compose.topology-e2e.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    services = compose["services"]

    assert {"api", "worker", "fixture", "neo4j", "qdrant"} <= set(services)
    assert services["api"]["command"] == ["agent-rag-serve"]
    assert services["worker"]["command"] == ["agent-rag-worker"]
    assert services["api"]["environment"]["INDEX_WORKER_ENABLED"] == "false"
    assert services["worker"]["environment"]["INDEX_WORKER_ENABLED"] == "true"
    assert (
        services["worker"]["environment"]["BUSINESS_E2E_CLAIM_DELAY_SECONDS"]
        == "20"
    )
    assert services["worker"]["environment"]["INDEX_WORKER_LEASE_SECONDS"] == "2"
    assert "agent-rag-worker-health" in services["worker"]["healthcheck"]["test"]
    assert "TOPOLOGY_API_AUTH_KEYS" in raw
    assert "topology-contract-reader" not in raw
    assert "topology-contract-admin" not in raw


def test_workflow_always_uploads_evidence_and_removes_isolated_volumes() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "production-topology-e2e.yml"
    ).read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agent-rag-topology-e2e" in workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in workflow
    assert "persist-credentials: false" in workflow
    assert "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "agent-rag-topology-e2e =" in pyproject
    assert "agent-rag-worker-health =" in pyproject
    assert "agent-rag-e2e-fixture =" in pyproject
