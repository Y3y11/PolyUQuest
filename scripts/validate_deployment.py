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

    required = {
        "api",
        "worker",
        "frontend",
        "neo4j",
        "qdrant",
        "backup",
        "restore",
        "otel-collector",
        "tempo",
    }
    missing = sorted(required - set(services))
    if missing:
        errors.append(f"missing services: {', '.join(missing)}")

    for name, service in services.items():
        image = str(service.get("image", ""))
        if image.endswith(":latest") or ":latest@" in image:
            errors.append(f"{name}: floating latest image is forbidden")

    for database in ("neo4j", "qdrant", "otel-collector", "tempo"):
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

    for name in ("otel-collector", "tempo"):
        service = services.get(name, {})
        if set(_list(service.get("networks"))) != {"backend"}:
            errors.append(f"{name}: must remain isolated on backend only")
        if service.get("profiles") != ["observability"]:
            errors.append(f"{name}: must be opt-in through the observability profile")

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

    for name in ("otel-collector", "tempo"):
        service = services.get(name, {})
        if service.get("user") != "10001:10001":
            errors.append(f"{name}: must run as uid/gid 10001")
        if service.get("read_only") is not True:
            errors.append(f"{name}: root filesystem must be read-only")
        if "ALL" not in _list(service.get("cap_drop")):
            errors.append(f"{name}: Linux capabilities must be dropped")
        if "no-new-privileges:true" not in _list(service.get("security_opt")):
            errors.append(f"{name}: no-new-privileges is required")
        if not service.get("mem_limit") or not service.get("cpus"):
            errors.append(f"{name}: CPU and memory limits are required")

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
    shared_admission_settings = (
        "AGENT_RUN_ADMISSION_ENABLED",
        "AGENT_RUN_ADMISSION_MAX_ACTIVE",
        "AGENT_RUN_ADMISSION_MAX_WAITING",
        "AGENT_RUN_ADMISSION_RETRY_AFTER_SECONDS",
        "AGENT_RUN_ADMISSION_WARN_RATIO",
        "AGENT_RUN_BUDGET_MAX_ITERATIONS",
        "AGENT_RUN_BUDGET_MAX_PAGES",
        "AGENT_RUN_BUDGET_MAX_SECONDS",
    )
    for name in shared_admission_settings:
        if name not in api_environment:
            errors.append(f"api must declare shared Agent Run policy {name}")
        elif api_environment.get(name) != worker_environment.get(name):
            errors.append(f"api and worker must share Agent Run policy {name}")
    metrics_settings = (
        "RUNTIME_METRICS_ENABLED",
        "RUNTIME_METRICS_CACHE_TTL_SECONDS",
        "RUNTIME_METRICS_TELEMETRY_WINDOW_HOURS",
        "RUNTIME_METRICS_WORKER_LIMIT",
    )
    for name in metrics_settings:
        if name not in api_environment:
            errors.append(f"api must declare runtime metrics policy {name}")
    trace_backend_settings = (
        "TRACE_BACKEND_ENABLED",
        "TRACE_BACKEND_URL",
        "TRACE_BACKEND_TIMEOUT_SECONDS",
        "TRACE_BACKEND_MAX_RESPONSE_BYTES",
        "TRACE_BACKEND_MAX_SPANS",
    )
    for name in trace_backend_settings:
        if name not in api_environment:
            errors.append(f"api must declare trace backend policy {name}")
    if api_environment.get("TRACE_BACKEND_URL") != (
        "${TRACE_BACKEND_URL:-http://tempo:3200}"
    ):
        errors.append("api trace backend must default to the private Tempo service")

    collector = services.get("otel-collector", {})
    tempo = services.get("tempo", {})
    if collector.get("image") != "otel/opentelemetry-collector-contrib:0.158.0":
        errors.append("otel-collector image must use the reviewed 0.158.0 release")
    if tempo.get("image") != "grafana/tempo:2.10.7":
        errors.append("tempo image must use the reviewed 2.10.7 release")
    if collector.get("command") != ["--config=/etc/otelcol-contrib/config.yaml"]:
        errors.append("otel-collector must load the repository privacy configuration")
    collector_config_path = root / "deploy" / "observability" / "otel-collector.yaml"
    try:
        collector_config = yaml.safe_load(
            collector_config_path.read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError):
        collector_config = {}
        errors.append("otel-collector privacy configuration is missing or invalid")
    redaction_config = (collector_config or {}).get("processors", {}).get(
        "redaction/privacy", {}
    )
    if "service.name" not in _list(redaction_config.get("allowed_keys")):
        errors.append("otel-collector privacy allowlist must preserve service.name")
    if tempo.get("command") != [
        "-config.file=/etc/tempo/tempo.yaml",
        "-target=all",
    ]:
        errors.append("tempo must run the reviewed monolithic configuration")

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
