"""Static deployment-policy checks that do not require a Docker daemon."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMPOSE = ROOT / "compose.production.yml"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_deployment(root: Path = ROOT) -> list[str]:
    compose_path = root / "compose.production.yml"
    payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = payload.get("services", {})
    errors: list[str] = []

    required = {"api", "worker", "frontend", "neo4j", "qdrant", "backup", "restore"}
    missing = sorted(required - set(services))
    if missing:
        errors.append(f"missing services: {', '.join(missing)}")

    for name, service in services.items():
        image = str(service.get("image", ""))
        if image.endswith(":latest") or ":latest@" in image:
            errors.append(f"{name}: floating latest image is forbidden")

    for database in ("neo4j", "qdrant"):
        if services.get(database, {}).get("ports"):
            errors.append(f"{database}: production database ports must not be published")

    if not payload.get("networks", {}).get("backend", {}).get("internal"):
        errors.append("backend network must be internal")

    for name in ("api", "worker"):
        networks = set(_list(services.get(name, {}).get("networks")))
        if not {"backend", "egress"}.issubset(networks):
            errors.append(f"{name}: must join private backend and outbound egress networks")
    for name in ("neo4j", "qdrant"):
        networks = set(_list(services.get(name, {}).get("networks")))
        if networks != {"backend"}:
            errors.append(f"{name}: database must remain isolated on backend only")
    frontend = services.get("frontend", {})
    frontend_networks = set(_list(frontend.get("networks")))
    if frontend_networks != {"frontend", "backend"}:
        errors.append("frontend: must join public frontend and private backend networks")

    for name in ("api", "worker", "frontend"):
        service = services.get(name, {})
        if service.get("user") != "10001:10001":
            errors.append(f"{name}: must run as uid/gid 10001")
        if service.get("read_only") is not True:
            errors.append(f"{name}: root filesystem must be read-only")
        if "ALL" not in _list(service.get("cap_drop")):
            errors.append(f"{name}: Linux capabilities must be dropped")
        if "no-new-privileges:true" not in _list(service.get("security_opt")):
            errors.append(f"{name}: no-new-privileges is required")
        if not service.get("stop_grace_period"):
            errors.append(f"{name}: stop_grace_period is required")

    api = services.get("api", {})
    worker = services.get("worker", {})
    if api.get("image") != worker.get("image"):
        errors.append("api and worker must use the same immutable application image")
    if worker.get("command") != ["agent-rag-worker"]:
        errors.append("worker must use the standalone agent-rag-worker entrypoint")
    api_environment = api.get("environment", {})
    worker_environment = worker.get("environment", {})
    expected_profile = "${BACKEND_RUNTIME_PROFILE:-remote}"
    for name, service in (("api", api), ("worker", worker)):
        environment = service.get("environment", {})
        build_args = service.get("build", {}).get("args", {})
        if environment.get("APP_RUNTIME_PROFILE") != expected_profile:
            errors.append(f"{name}: runtime profile must use BACKEND_RUNTIME_PROFILE")
        if build_args.get("APP_RUNTIME_PROFILE") != expected_profile:
            errors.append(f"{name}: image build profile must use BACKEND_RUNTIME_PROFILE")
    if api_environment.get("APP_PROCESS_ROLE") != "api":
        errors.append("api must declare APP_PROCESS_ROLE=api")
    if worker_environment.get("APP_PROCESS_ROLE") != "worker":
        errors.append("worker must declare APP_PROCESS_ROLE=worker")
    if "API_AUTH_KEYS" in worker_environment:
        errors.append("worker must not receive API_AUTH_KEYS")
    if worker_environment.get("API_AUTH_MODE") != "disabled":
        errors.append("worker HTTP auth must remain disabled because it serves no API")
    if api_environment.get("INDEX_WORKER_ENABLED") != "false":
        errors.append("api must disable the in-process index worker")
    if api_environment.get("FRESHNESS_WORKER_ENABLED") != "false":
        errors.append("api must disable the in-process freshness worker")
    if api_environment.get("AGENT_RUN_WORKER_ENABLED") != "false":
        errors.append("api must disable the in-process Agent Run worker")
    if worker_environment.get("AGENT_RUN_WORKER_ENABLED") != "true":
        errors.append("worker must enable the Agent Run worker")
    expected_run_store = "/app/data/runtime/agent_runs.sqlite3"
    if api_environment.get("AGENT_RUN_STORE_PATH") != expected_run_store:
        errors.append("api must use the shared Agent Run store path")
    if worker_environment.get("AGENT_RUN_STORE_PATH") != expected_run_store:
        errors.append("worker must use the shared Agent Run store path")

    frontend_environment = frontend.get("environment", {})
    if frontend_environment.get("BACKEND_API_URL") != "http://api:8000/api":
        errors.append("frontend: BACKEND_API_URL must use the private API service")
    if frontend_environment.get("BFF_BACKEND_API_KEY_FILE") != (
        "/run/secrets/bff_backend_api_key"
    ):
        errors.append("frontend: BFF key must be read from the mounted secret file")
    if "BFF_BACKEND_API_KEY" in frontend_environment:
        errors.append("frontend: raw BFF key must not be stored in container environment")
    if "bff_backend_api_key" not in _list(frontend.get("secrets")):
        errors.append("frontend: bff_backend_api_key secret mount is required")
    secret = payload.get("secrets", {}).get("bff_backend_api_key", {})
    if "${BFF_BACKEND_API_KEY_FILE:?" not in str(secret.get("file", "")):
        errors.append("bff_backend_api_key must require an operator-provided host file")

    compose_text = compose_path.read_text(encoding="utf-8")
    required_variables = (
        "${API_AUTH_KEYS:?",
        "${NEO4J_PASSWORD:?",
        "${CORS_ALLOW_ORIGINS:?",
        "${BFF_ALLOWED_ORIGINS:?",
        "${BFF_BACKEND_API_KEY_FILE:?",
    )
    for marker in required_variables:
        if marker not in compose_text:
            errors.append(f"required production variable guard missing: {marker}")

    for dockerfile in (root / "Dockerfile", root / "frontend" / "Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        if "USER 10001:10001" not in text:
            errors.append(f"{dockerfile.name}: non-root USER directive missing")
        if ":latest" in text:
            errors.append(f"{dockerfile.name}: floating latest image is forbidden")
    backend_dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    for marker in (
        "ARG APP_RUNTIME_PROFILE=remote",
        "/app/.runtime-profile",
        "org.polyuquest.runtime-profile",
    ):
        if marker not in backend_dockerfile:
            errors.append(f"Dockerfile: runtime profile marker missing: {marker}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors = validate_deployment(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("deployment policy validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
