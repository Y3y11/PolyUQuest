"""Centralised configuration loaded from .env + YAML files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings

from agent_rag.security.credentials import parse_api_key_records

load_dotenv()

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGS = _ROOT / "configs"


def _load_yaml(name: str) -> dict[str, Any]:
    path = _CONFIGS / name
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class Settings(BaseSettings):
    # Deployment and API security boundary
    app_environment: Literal["development", "test", "production"] = "development"
    app_process_role: Literal["api", "worker"] = "api"
    app_runtime_profile: Literal["auto", "remote", "local-ml"] = "auto"
    api_auth_mode: Literal["disabled", "api_key"] = "disabled"
    # Comma-separated key_id:role:sha256 entries. Raw keys never belong here.
    api_auth_keys: str = ""
    security_audit_path: str = "data/runtime/security_audit.sqlite3"
    security_audit_retention_days: int = 90

    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    llm_provider: str = "siliconflow"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "agent_rag_polyu"

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Embedding
    embedding_provider: str = "siliconflow"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_concurrency: int = 8
    embedding_max_retries: int = 2

    # Crawler
    crawl_delay_seconds: float = 1.5
    firecrawl_api_key: str = ""
    jina_api_key: str = ""

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = False
    # Query-driven discovery is a live indexing path: fetched trusted pages
    # are persisted through audited graph patches unless an environment
    # explicitly disables writes.
    agent_allow_persistence: bool = True
    agent_ledger_path: str = "data/runtime/agent_ledger.sqlite3"
    agent_repair_on_startup: bool = True
    agent_repair_max_patches: int = 25
    agent_patch_max_attempts: int = 5
    agent_async_indexing: bool = True
    agent_run_store_path: str = "data/runtime/agent_runs.sqlite3"
    agent_run_worker_enabled: bool = True
    agent_run_worker_poll_seconds: float = 0.25
    agent_run_lease_seconds: int = 120
    agent_run_max_attempts: int = 2
    agent_run_retry_base_seconds: float = 2.0
    agent_run_retention_days: int = 7
    agent_run_event_poll_seconds: float = 0.25
    agent_run_sse_keepalive_seconds: float = 15.0
    agent_run_queue_warn_seconds: float = 30.0
    agent_run_queue_critical_seconds: float = 120.0
    agent_run_admission_enabled: bool = True
    agent_run_admission_max_active: int = 100
    agent_run_admission_max_waiting: int = 80
    agent_run_admission_retry_after_seconds: int = 5
    agent_run_admission_warn_ratio: float = 0.8
    agent_run_budget_max_iterations: int = 5
    agent_run_budget_max_pages: int = 10
    agent_run_budget_max_seconds: int = 120
    runtime_metrics_enabled: bool = True
    runtime_metrics_cache_ttl_seconds: float = 5.0
    runtime_metrics_telemetry_window_hours: int = 24
    runtime_metrics_worker_limit: int = 100
    otel_tracing_mode: Literal["disabled", "propagate", "otlp"] = "propagate"
    otel_service_name: str = "polyuquest-api"
    otel_traces_sampler_arg: float = 0.1
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: str = ""
    trace_backend_enabled: bool = False
    trace_backend_url: str = "http://tempo:3200"
    trace_backend_timeout_seconds: float = 3.0
    trace_backend_max_response_bytes: int = 2_097_152
    trace_backend_max_spans: int = 500
    index_worker_enabled: bool = True
    index_worker_poll_seconds: float = 0.5
    index_worker_lease_seconds: int = 120
    index_job_max_attempts: int = 5
    index_job_retry_base_seconds: float = 2.0
    index_job_retry_max_seconds: float = 300.0
    index_job_retention_days: int = 30
    freshness_worker_enabled: bool = True
    freshness_worker_poll_seconds: float = 5.0
    freshness_worker_lease_seconds: int = 120
    worker_shutdown_grace_seconds: float = 30.0
    worker_heartbeat_seconds: float = 5.0
    worker_heartbeat_max_age_seconds: float = 20.0

    # Deterministic, test-only runtime composition for the production-topology
    # E2E gate. Non-test environments fail fast if this boundary is enabled.
    business_e2e_mode: Literal["disabled", "topology"] = "disabled"
    business_e2e_token: str = ""
    business_e2e_fixture_origin: str = ""
    business_e2e_claim_delay_seconds: float = 0.0
    business_e2e_claim_marker: str = "data/runtime/topology-claim-delay.done"
    business_e2e_agent_run_delay_seconds: float = 0.0
    business_e2e_agent_run_delay_marker: str = (
        "data/runtime/topology-agent-run-delay.done"
    )

    # CORS
    # 逗号分隔的 origin 白名单。生产环境务必改为明确域名，例如
    # "https://app.polyu.edu.hk,https://admin.polyu.edu.hk"
    cors_allow_origins: str = (
        "http://localhost:5173,http://localhost:3000,http://localhost:8080,"
        "http://127.0.0.1:5173,http://127.0.0.1:3000,http://127.0.0.1:8080"
    )
    # 可选：正则匹配一族 origin（如多子域）。非空时覆盖 cors_allow_origins。
    # 例： r"https://([a-z0-9-]+\.)?polyu\.edu\.hk"
    cors_allow_origin_regex: str = ""
    cors_allow_credentials: bool = True
    cors_allow_methods: str = "*"
    cors_allow_headers: str = "*"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def cors_methods_list(self) -> list[str]:
        raw = self.cors_allow_methods.strip()
        if raw == "*":
            return ["*"]
        return [m.strip() for m in raw.split(",") if m.strip()]

    @property
    def cors_headers_list(self) -> list[str]:
        raw = self.cors_allow_headers.strip()
        if raw == "*":
            return ["*"]
        return [h.strip() for h in raw.split(",") if h.strip()]

    @model_validator(mode="after")
    def _validate_runtime(self) -> Settings:
        """启动期校验 Worker、安全边界与 CORS，防止误配上线。

        规则：
        1. Worker lease/retry、审计 retention 必须为正数。
        2. 生产环境禁止匿名 API，API-key 模式必须有合法 admin key。
        3. credentials=True 时，白名单禁止通配 "*"（浏览器会直接拒绝）。
        4. credentials=True 时，origin_regex 禁止使用等价于通配的过宽模式。
        5. origin_regex 非空时必须能被 re.compile 通过，否则延后到
           Starlette 挂载中间件时才报错，栈信息对排查不友好。
        """
        origins = self.cors_origins_list
        regex = self.cors_allow_origin_regex.strip()

        from agent_rag.deployment.runtime_profile import validate_runtime_capabilities

        validate_runtime_capabilities(
            declared_profile=self.app_runtime_profile,
            embedding_provider=self.embedding_provider,
        )

        if self.index_worker_enabled and not self.agent_async_indexing:
            raise ValueError(
                "INDEX_WORKER_ENABLED=true requires AGENT_ASYNC_INDEXING=true"
            )
        if self.agent_run_worker_poll_seconds <= 0:
            raise ValueError("AGENT_RUN_WORKER_POLL_SECONDS must be positive")
        if self.agent_run_lease_seconds <= 0:
            raise ValueError("AGENT_RUN_LEASE_SECONDS must be positive")
        if self.agent_run_max_attempts <= 0:
            raise ValueError("AGENT_RUN_MAX_ATTEMPTS must be positive")
        if self.agent_run_retry_base_seconds < 0:
            raise ValueError("AGENT_RUN_RETRY_BASE_SECONDS cannot be negative")
        if self.agent_run_retention_days <= 0:
            raise ValueError("AGENT_RUN_RETENTION_DAYS must be positive")
        if self.agent_run_event_poll_seconds <= 0:
            raise ValueError("AGENT_RUN_EVENT_POLL_SECONDS must be positive")
        if self.agent_run_sse_keepalive_seconds <= 0:
            raise ValueError("AGENT_RUN_SSE_KEEPALIVE_SECONDS must be positive")
        if self.agent_run_queue_warn_seconds <= 0:
            raise ValueError("AGENT_RUN_QUEUE_WARN_SECONDS must be positive")
        if self.agent_run_queue_critical_seconds <= self.agent_run_queue_warn_seconds:
            raise ValueError(
                "AGENT_RUN_QUEUE_CRITICAL_SECONDS must be greater than "
                "AGENT_RUN_QUEUE_WARN_SECONDS"
            )
        if self.agent_run_admission_max_active <= 0:
            raise ValueError("AGENT_RUN_ADMISSION_MAX_ACTIVE must be positive")
        if self.agent_run_admission_max_waiting <= 0:
            raise ValueError("AGENT_RUN_ADMISSION_MAX_WAITING must be positive")
        if (
            self.agent_run_admission_max_waiting
            > self.agent_run_admission_max_active
        ):
            raise ValueError(
                "AGENT_RUN_ADMISSION_MAX_WAITING cannot exceed "
                "AGENT_RUN_ADMISSION_MAX_ACTIVE"
            )
        if self.agent_run_admission_retry_after_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_ADMISSION_RETRY_AFTER_SECONDS must be positive"
            )
        if not 0 < self.agent_run_admission_warn_ratio < 1:
            raise ValueError(
                "AGENT_RUN_ADMISSION_WARN_RATIO must be between 0 and 1"
            )
        from agent_rag.limits import AGENT_BUDGET_LIMITS

        configured_budget_limits = {
            "AGENT_RUN_BUDGET_MAX_ITERATIONS": (
                self.agent_run_budget_max_iterations,
                AGENT_BUDGET_LIMITS["max_iterations"],
            ),
            "AGENT_RUN_BUDGET_MAX_PAGES": (
                self.agent_run_budget_max_pages,
                AGENT_BUDGET_LIMITS["max_pages"],
            ),
            "AGENT_RUN_BUDGET_MAX_SECONDS": (
                self.agent_run_budget_max_seconds,
                AGENT_BUDGET_LIMITS["max_seconds"],
            ),
        }
        for name, (configured, public_limit) in configured_budget_limits.items():
            if configured <= 0 or configured > public_limit:
                raise ValueError(
                    f"{name} must be positive and no greater than {public_limit}"
                )
        if not 1 <= self.runtime_metrics_cache_ttl_seconds <= 60:
            raise ValueError(
                "RUNTIME_METRICS_CACHE_TTL_SECONDS must be between 1 and 60"
            )
        if not 1 <= self.runtime_metrics_telemetry_window_hours <= 2160:
            raise ValueError(
                "RUNTIME_METRICS_TELEMETRY_WINDOW_HOURS must be between 1 and 2160"
            )
        if not 1 <= self.runtime_metrics_worker_limit <= 500:
            raise ValueError("RUNTIME_METRICS_WORKER_LIMIT must be between 1 and 500")
        if not 0 <= self.otel_traces_sampler_arg <= 1:
            raise ValueError("OTEL_TRACES_SAMPLER_ARG must be between 0 and 1")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,63}", self.otel_service_name):
            raise ValueError("OTEL_SERVICE_NAME has an invalid format")
        if self.otel_tracing_mode == "otlp" and not self.otel_exporter_otlp_endpoint:
            raise ValueError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_TRACING_MODE=otlp"
            )
        trace_backend = urlsplit(self.trace_backend_url)
        if (
            trace_backend.scheme not in {"http", "https"}
            or not trace_backend.hostname
            or trace_backend.username is not None
            or trace_backend.password is not None
            or trace_backend.query
            or trace_backend.fragment
            or trace_backend.path not in {"", "/"}
        ):
            raise ValueError(
                "TRACE_BACKEND_URL must be an http(s) origin without credentials, "
                "path, query, or fragment"
            )
        if not 0.1 <= self.trace_backend_timeout_seconds <= 30:
            raise ValueError(
                "TRACE_BACKEND_TIMEOUT_SECONDS must be between 0.1 and 30"
            )
        if not 1024 <= self.trace_backend_max_response_bytes <= 10_485_760:
            raise ValueError(
                "TRACE_BACKEND_MAX_RESPONSE_BYTES must be between 1024 and 10485760"
            )
        if not 1 <= self.trace_backend_max_spans <= 5000:
            raise ValueError("TRACE_BACKEND_MAX_SPANS must be between 1 and 5000")
        if self.index_worker_lease_seconds <= 0:
            raise ValueError("INDEX_WORKER_LEASE_SECONDS must be positive")
        if self.index_job_max_attempts <= 0:
            raise ValueError("INDEX_JOB_MAX_ATTEMPTS must be positive")
        if self.freshness_worker_poll_seconds <= 0:
            raise ValueError("FRESHNESS_WORKER_POLL_SECONDS must be positive")
        if self.freshness_worker_lease_seconds <= 0:
            raise ValueError("FRESHNESS_WORKER_LEASE_SECONDS must be positive")
        if self.worker_shutdown_grace_seconds <= 0:
            raise ValueError("WORKER_SHUTDOWN_GRACE_SECONDS must be positive")
        if self.worker_heartbeat_seconds <= 0:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be positive")
        if self.worker_heartbeat_max_age_seconds < self.worker_heartbeat_seconds:
            raise ValueError(
                "WORKER_HEARTBEAT_MAX_AGE_SECONDS must be greater than or equal "
                "to WORKER_HEARTBEAT_SECONDS"
            )
        if self.business_e2e_claim_delay_seconds < 0:
            raise ValueError("BUSINESS_E2E_CLAIM_DELAY_SECONDS cannot be negative")
        if self.business_e2e_agent_run_delay_seconds < 0:
            raise ValueError(
                "BUSINESS_E2E_AGENT_RUN_DELAY_SECONDS cannot be negative"
            )
        if self.business_e2e_mode != "disabled":
            if self.app_environment != "test":
                raise ValueError("BUSINESS_E2E_MODE is only allowed in APP_ENVIRONMENT=test")
            if not re.fullmatch(r"[a-zA-Z0-9._-]{3,80}", self.business_e2e_token):
                raise ValueError("BUSINESS_E2E_TOKEN has an invalid format")
            if not re.fullmatch(
                r"https?://[a-zA-Z0-9._-]+(?::[0-9]{1,5})?",
                self.business_e2e_fixture_origin,
            ):
                raise ValueError("BUSINESS_E2E_FIXTURE_ORIGIN must be an HTTP origin")
        elif (
            self.business_e2e_claim_delay_seconds
            or self.business_e2e_agent_run_delay_seconds
        ):
            raise ValueError(
                "BUSINESS_E2E delay injection requires BUSINESS_E2E_MODE=topology"
            )
        if self.security_audit_retention_days <= 0:
            raise ValueError("SECURITY_AUDIT_RETENTION_DAYS must be positive")
        if (
            self.app_environment == "production"
            and self.app_process_role == "api"
            and self.api_auth_mode == "disabled"
        ):
            raise ValueError(
                "API_AUTH_MODE=disabled is not allowed in APP_ENVIRONMENT=production"
            )
        if (
            self.app_environment == "production"
            and self.app_process_role == "api"
            and self.agent_run_worker_enabled
        ):
            raise ValueError(
                "production API must set AGENT_RUN_WORKER_ENABLED=false"
            )
        if self.app_environment == "production":
            if self.app_runtime_profile == "auto":
                raise ValueError(
                    "production requires explicit APP_RUNTIME_PROFILE=remote or local-ml"
                )
            if self.api_reload:
                raise ValueError("API_RELOAD=true is not allowed in production")
            if not self.neo4j_password or self.neo4j_password == "agent_rag_polyu":
                raise ValueError(
                    "production requires a non-default NEO4J_PASSWORD"
                )
            provider_keys = {
                "deepseek": self.deepseek_api_key,
                "qwen": self.qwen_api_key,
                "siliconflow": self.siliconflow_api_key,
            }
            if self.llm_provider not in provider_keys:
                raise ValueError(f"unsupported production LLM_PROVIDER={self.llm_provider}")
            if self.llm_provider in provider_keys and not provider_keys[self.llm_provider]:
                raise ValueError(
                    f"LLM_PROVIDER={self.llm_provider} requires its API key in production"
                )
            if self.embedding_provider not in {"local", "siliconflow", "api"}:
                raise ValueError(
                    f"unsupported production EMBEDDING_PROVIDER={self.embedding_provider}"
                )
            if self.embedding_provider == "siliconflow" and not self.siliconflow_api_key:
                raise ValueError(
                    "EMBEDDING_PROVIDER=siliconflow requires SILICONFLOW_API_KEY "
                    "in production"
                )
            if self.embedding_provider == "api" and (
                not self.embedding_api_key or not self.embedding_base_url
            ):
                raise ValueError(
                    "EMBEDDING_PROVIDER=api requires EMBEDDING_API_KEY and "
                    "EMBEDDING_BASE_URL in production"
                )
        if self.api_auth_mode == "api_key":
            parse_api_key_records(self.api_auth_keys, require_admin=True)

        if self.cors_allow_credentials:
            if "*" in origins:
                raise ValueError(
                    "CORS_ALLOW_ORIGINS 不能包含 '*' —— 浏览器拒绝通配 origin "
                    "与 CORS_ALLOW_CREDENTIALS=true 同时出现。请列出明确域名，"
                    "或设置 CORS_ALLOW_CREDENTIALS=false。"
                )
            _unsafe_regex = {".*", ".+", "^.*$", "^.+$", "^.*", ".*$"}
            if regex in _unsafe_regex:
                raise ValueError(
                    f"CORS_ALLOW_ORIGIN_REGEX={regex!r} 等价于放开全部 origin，"
                    "与 CORS_ALLOW_CREDENTIALS=true 组合不安全。请改为受限的域名模式。"
                )

        if regex:
            try:
                re.compile(regex)
            except re.error as exc:
                raise ValueError(
                    f"CORS_ALLOW_ORIGIN_REGEX 不是合法正则: {exc}"
                ) from exc

        return self


settings = Settings()

crawl_config = _load_yaml("crawl.yaml")
llm_config = _load_yaml("llm.yaml")
aliases_config = _load_yaml("aliases.yaml")
thresholds_config = _load_yaml("thresholds.yaml")
agent_config = _load_yaml("agent.yaml")
observability_config = _load_yaml("observability.yaml")


def stage_model(stage: str) -> str | None:
    """Resolve a stage model, allowing deployment-specific env overrides."""
    override = os.getenv(f"{stage.upper()}_MODEL", "").strip()
    if override:
        return override
    return (llm_config.get(stage, {}) or {}).get("model")
