"""Centralised configuration loaded from .env + YAML files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings

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
    # Durable graph mutation from the Agent API is opt-in even when a request
    # sets persist_discoveries=true.
    agent_allow_persistence: bool = False

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
    def _validate_cors(self) -> "Settings":
        """启动期校验 CORS 配置，防止误配重新出现。

        规则：
        1. credentials=True 时，白名单禁止通配 "*"（浏览器会直接拒绝）。
        2. credentials=True 时，origin_regex 禁止使用等价于通配的过宽模式。
        3. origin_regex 非空时必须能被 re.compile 通过，否则延后到
           Starlette 挂载中间件时才报错，栈信息对排查不友好。
        """
        origins = self.cors_origins_list
        regex = self.cors_allow_origin_regex.strip()

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


def stage_model(stage: str) -> str | None:
    """Resolve a stage model, allowing deployment-specific env overrides."""
    override = os.getenv(f"{stage.upper()}_MODEL", "").strip()
    if override:
        return override
    return (llm_config.get(stage, {}) or {}).get("model")
