from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agent_rag.config import Settings
from agent_rag.deployment import runtime_profile
from agent_rag.e2e import topology_runtime
from agent_rag.e2e.fixture_app import app, state
from agent_rag.e2e.topology_driver import TopologyE2EDriver
from agent_rag.e2e.topology_runtime import _DelayOnceAgent, _initialize_backends

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

    with (
        patch.object(runtime_profile, "_module_available", return_value=False),
        pytest.raises(ValueError, match="requires BUSINESS_E2E_MODE"),
    ):
        Settings(
            _env_file=None,
            app_environment="test",
            app_runtime_profile="remote",
            embedding_provider="siliconflow",
            business_e2e_agent_run_delay_seconds=1,
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


def test_fixture_container_entrypoint_has_no_storage_import_chain() -> None:
    fixture_app = (ROOT / "src" / "agent_rag" / "e2e" / "fixture_app.py").read_text(
        encoding="utf-8"
    )
    fixture_content = (
        ROOT / "src" / "agent_rag" / "e2e" / "fixture_content.py"
    ).read_text(encoding="utf-8")

    assert "from agent_rag.e2e.fixture_content import fixture_html" in fixture_app
    assert "agent_rag.e2e.site" not in fixture_app
    assert "agent_rag.tools" not in fixture_content


def test_topology_backend_initialization_retries_startup_race() -> None:
    first = MagicMock()
    first.init_all.side_effect = RuntimeError("neo4j is starting")
    second = MagicMock()

    with (
        patch(
            "agent_rag.e2e.topology_runtime.GraphVectorStore",
            side_effect=[first, second],
        ),
        patch("agent_rag.e2e.topology_runtime.time.sleep") as sleep,
    ):
        _initialize_backends(timeout_seconds=1, retry_seconds=0)

    first.close.assert_called_once_with()
    second.close.assert_called_once_with()
    sleep.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_topology_agent_delay_is_async_and_marker_guarded(
    tmp_path: Path,
) -> None:
    class Delegate:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return "done"

    delegate = Delegate()
    delayed = _DelayOnceAgent(delegate)  # type: ignore[arg-type]
    marker = tmp_path / "agent-delay.done"
    with (
        patch.object(
            topology_runtime.settings,
            "business_e2e_agent_run_delay_seconds",
            5,
        ),
        patch.object(
            topology_runtime.settings,
            "business_e2e_agent_run_delay_marker",
            str(marker),
        ),
        patch("agent_rag.e2e.topology_runtime.asyncio.sleep") as sleep,
    ):
        assert await delayed.run() == "done"
        assert await delayed.run() == "done"

    sleep.assert_awaited_once_with(5)
    assert delegate.calls == 2
    assert marker.read_text(encoding="utf-8") == "delay_seconds=5\n"


def test_topology_driver_parses_audited_sse_contract(tmp_path: Path) -> None:
    driver = TopologyE2EDriver(
        compose_file=ROOT / "compose.topology-e2e.yml",
        project="polyuquest-contract",
        token="topology-contract",
        api_base="http://api.test",
        fixture_base="http://fixture.test",
        browser_base="http://browser.test",
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


def test_topology_driver_parses_durable_cursor_replay(tmp_path: Path) -> None:
    driver = TopologyE2EDriver(
        compose_file=ROOT / "compose.topology-e2e.yml",
        project="polyuquest-durable-contract",
        token="topology-contract",
        api_base="http://api.test",
        fixture_base="http://fixture.test",
        browser_base="http://browser.test",
        output=tmp_path / "report.json",
        timeout_seconds=1,
        build=False,
    )
    driver.client.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["last-event-id"] == "7"
        assert "x-api-key" not in request.headers
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-BFF-Request-ID": "bff-durable",
                "X-Request-ID": "api-durable",
            },
            text=(
                'id: 8\nevent: run_attempt_started\ndata: {"attempt":2}\n\n'
                'id: 9\nevent: done\ndata: {"run_id":"run-contract"}\n\n'
            ),
        )

    driver.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        events, headers = driver._durable_events("run-contract", after=7)
    finally:
        driver.client.close()

    assert [event["id"] for event in events] == [8, 9]
    assert [event["event"] for event in events] == [
        "run_attempt_started",
        "done",
    ]
    assert headers["x-bff-request-id"] == "bff-durable"


def test_topology_driver_creates_run_through_bff_without_browser_key(
    tmp_path: Path,
) -> None:
    driver = TopologyE2EDriver(
        compose_file=ROOT / "compose.topology-e2e.yml",
        project="polyuquest-bff-contract",
        token="topology-contract",
        api_base="http://api.test",
        fixture_base="http://fixture.test",
        browser_base="http://browser.test",
        output=tmp_path / "report.json",
        timeout_seconds=1,
        build=False,
    )
    driver.client.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent/runs"
        assert request.headers["origin"] == "http://browser.test"
        assert request.headers["idempotency-key"] == (
            "topology-topology-contract-durable-run"
        )
        assert "x-api-key" not in request.headers
        return httpx.Response(
            202,
            headers={
                "X-BFF-Request-ID": "bff-submit",
                "X-Request-ID": "api-submit",
            },
            json={
                "run_id": "run-0123456789abcdef0123456789abcdef",
                "status": "queued",
                "created": True,
            },
        )

    driver.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        submission, headers = driver._create_durable_run()
    finally:
        driver.client.close()

    assert submission["created"] is True
    assert headers == {
        "x-bff-request-id": "bff-submit",
        "x-request-id": "api-submit",
    }


def test_compose_encodes_split_process_and_claim_takeover() -> None:
    raw = (ROOT / "compose.topology-e2e.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    services = compose["services"]

    assert {"api", "worker", "frontend", "fixture", "neo4j", "qdrant"} <= set(
        services
    )
    assert services["api"]["command"] == ["agent-rag-serve"]
    assert services["worker"]["command"] == ["agent-rag-worker"]
    assert services["api"]["environment"]["INDEX_WORKER_ENABLED"] == "false"
    assert services["api"]["environment"]["AGENT_RUN_WORKER_ENABLED"] == "false"
    assert services["worker"]["environment"]["INDEX_WORKER_ENABLED"] == "true"
    assert services["worker"]["environment"]["AGENT_RUN_WORKER_ENABLED"] == "true"
    assert services["api"]["environment"]["AGENT_RUN_ADMISSION_ENABLED"] == "true"
    assert services["api"]["environment"]["AGENT_RUN_ADMISSION_MAX_ACTIVE"] == "100"
    assert services["api"]["environment"]["AGENT_RUN_ADMISSION_MAX_WAITING"] == "80"
    assert services["worker"]["environment"]["AGENT_RUN_ADMISSION_MAX_ACTIVE"] == "100"
    assert services["worker"]["environment"]["AGENT_RUN_LEASE_SECONDS"] == "2"
    assert (
        services["worker"]["environment"]["BUSINESS_E2E_CLAIM_DELAY_SECONDS"]
        == "20"
    )
    assert services["worker"]["environment"]["INDEX_WORKER_LEASE_SECONDS"] == "2"
    assert (
        services["worker"]["environment"][
            "BUSINESS_E2E_AGENT_RUN_DELAY_SECONDS"
        ]
        == "20"
    )
    assert services["frontend"]["environment"]["BACKEND_API_URL"] == (
        "http://api:8000/api"
    )
    assert "topology_bff_api_key" in services["frontend"]["secrets"]
    assert "topology_gateway_identity_secret" in services["frontend"]["secrets"]
    assert "topology_internal_identity_secret" in services["frontend"]["secrets"]
    assert "topology_internal_identity_secret" in services["api"]["secrets"]
    assert services["api"]["environment"]["END_USER_IDENTITY_MODE"] == "signed_jwt"
    assert "agent-rag-worker-health" in services["worker"]["healthcheck"]["test"]
    assert services["api"]["depends_on"]["neo4j"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["neo4j"]["condition"] == "service_healthy"
    assert "cypher-shell" in " ".join(services["neo4j"]["healthcheck"]["test"])
    assert "topology_neo4j_data:/data" in services["neo4j"]["volumes"]
    assert "topology_neo4j_logs:/logs" in services["neo4j"]["volumes"]
    assert "topology_neo4j_data" in compose["volumes"]
    assert "topology_neo4j_logs" in compose["volumes"]
    assert "TOPOLOGY_API_AUTH_KEYS" in raw
    assert "TOPOLOGY_BFF_API_KEY_FILE" in raw
    assert "TOPOLOGY_GATEWAY_IDENTITY_SECRET_FILE" in raw
    assert "TOPOLOGY_INTERNAL_IDENTITY_SECRET_FILE" in raw
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
    assert "neo4j-health.json" in workflow
    assert "neo4j-debug.log" in workflow
    assert "tail -n 500 /logs/debug.log" in workflow
    assert "persist-credentials: false" in workflow
    assert "Create ephemeral topology identity and BFF secrets" in workflow
    assert "Remove ephemeral topology identity and BFF secrets" in workflow
    assert "root:10001" in workflow
    assert "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "agent-rag-topology-e2e =" in pyproject
    assert "agent-rag-worker-health =" in pyproject
    assert "agent-rag-e2e-fixture =" in pyproject
