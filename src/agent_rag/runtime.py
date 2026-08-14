"""Runtime composition boundary for API and independent Worker processes."""

from __future__ import annotations

from typing import Any

from agent_rag.config import settings


def _topology_e2e_enabled() -> bool:
    return settings.business_e2e_mode == "topology"


def prepare_process_runtime() -> None:
    if _topology_e2e_enabled():
        from agent_rag.e2e.topology_runtime import prepare_process_runtime as prepare

        prepare()


def build_query_agent() -> Any:
    if _topology_e2e_enabled():
        from agent_rag.e2e.topology_runtime import build_query_agent as build

        return build()
    from agent_rag.agent.orchestrator import QueryDrivenAgent

    return QueryDrivenAgent()


def build_publish_patch_tool() -> Any:
    if _topology_e2e_enabled():
        from agent_rag.e2e.topology_runtime import build_publish_patch_tool as build

        return build()
    from agent_rag.tools.graph_patch import PublishPatchTool

    return PublishPatchTool()


def build_freshness_worker() -> Any:
    if _topology_e2e_enabled():
        from agent_rag.e2e.topology_runtime import build_freshness_worker as build

        return build()
    from agent_rag.freshness.worker import FreshnessWorker

    return FreshnessWorker()
