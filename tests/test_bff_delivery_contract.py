from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_production_frontend_uses_private_bff_network_and_secret_file() -> None:
    compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
    frontend = compose["services"]["frontend"]

    assert set(frontend["networks"]) == {"frontend", "backend"}
    assert frontend["environment"]["BACKEND_API_URL"] == "http://api:8000/api"
    assert (
        frontend["environment"]["BFF_BACKEND_API_KEY_FILE"]
        == "/run/secrets/bff_backend_api_key"
    )
    assert "BFF_BACKEND_API_KEY" not in frontend["environment"]
    assert frontend["secrets"] == ["bff_backend_api_key"]
    assert "BFF_BACKEND_API_KEY_FILE" in compose["secrets"]["bff_backend_api_key"]["file"]


def test_client_bundle_source_cannot_select_or_embed_backend() -> None:
    api_source = (ROOT / "frontend" / "lib" / "api.ts").read_text(encoding="utf-8")
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert 'const API_BASE = "/api"' in api_source
    assert "NEXT_PUBLIC_API_URL" not in api_source
    assert "NEXT_PUBLIC_API_URL" not in dockerfile
    assert "http://localhost:8000" not in api_source


def test_bff_e2e_compose_is_isolated_and_uses_docker_secret() -> None:
    compose = yaml.safe_load((ROOT / "compose.bff-e2e.yml").read_text(encoding="utf-8"))
    frontend = compose["services"]["frontend"]
    upstream = compose["services"]["upstream"]

    assert compose["networks"]["bff_backend"]["internal"] is True
    assert set(frontend["networks"]) == {"bff_frontend", "bff_backend"}
    assert frontend["read_only"] is True
    assert "ALL" in frontend["cap_drop"]
    assert frontend["secrets"] == ["bff_backend_api_key"]
    assert frontend["environment"]["BACKEND_API_URL"] == "http://upstream:18081/api"
    assert "BFF_BACKEND_API_KEY" not in frontend["environment"]
    assert upstream["read_only"] is True
    assert set(upstream["networks"]) == {"bff_backend", "bff_control"}
    assert "BFF_E2E_EXPECTED_KEY_SHA256" in upstream["environment"]


def test_bff_workflow_runs_build_scenario_artifacts_and_cleanup() -> None:
    workflow = (ROOT / ".github" / "workflows" / "browser-bff-e2e.yml").read_text(
        encoding="utf-8"
    )

    assert "npm test" in workflow
    assert "npm exec tsc -- --noEmit --incremental false" in workflow
    assert "npm run build" in workflow
    assert "openssl rand -hex 32" in workflow
    assert 'sudo chown root:10001 "$secret_file"' in workflow
    assert "sudo chmod 0440" in workflow
    assert "node frontend/scripts/bff-e2e-driver.mjs" in workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in workflow
    assert '"$RUNNER_TEMP"/*) sudo rm -f' in workflow
    assert "persist-credentials: false" in workflow
    assert "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
