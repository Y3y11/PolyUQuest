"""Runtime capability profile resolved from image marker and configuration."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RuntimeProfile = Literal["auto", "remote", "local-ml"]
IMAGE_PROFILE_MARKER = Path("/app/.runtime-profile")
KNOWN_PROFILES = {"remote", "local-ml"}
LOCAL_ML_MODULE = "sentence_transformers"
UNUSED_FLAG_MODULE = "FlagEmbedding"


@dataclass(frozen=True)
class RuntimeCapabilities:
    declared_profile: RuntimeProfile
    marker_profile: str | None
    effective_profile: RuntimeProfile
    local_ml_available: bool
    torch_available: bool
    flag_embedding_available: bool


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _read_marker(marker_path: Path) -> str | None:
    try:
        value = marker_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if value not in KNOWN_PROFILES:
        raise ValueError(
            f"invalid immutable runtime profile marker {value!r}; "
            "rebuild the backend image"
        )
    return value


def inspect_runtime_capabilities(
    declared_profile: RuntimeProfile,
    *,
    marker_path: Path = IMAGE_PROFILE_MARKER,
) -> RuntimeCapabilities:
    marker_profile = _read_marker(marker_path)
    if marker_profile is not None and declared_profile not in {"auto", marker_profile}:
        raise ValueError(
            "APP_RUNTIME_PROFILE does not match the immutable image profile: "
            f"declared={declared_profile}, image={marker_profile}"
        )
    effective_profile: RuntimeProfile = marker_profile or declared_profile
    return RuntimeCapabilities(
        declared_profile=declared_profile,
        marker_profile=marker_profile,
        effective_profile=effective_profile,
        local_ml_available=_module_available(LOCAL_ML_MODULE),
        torch_available=_module_available("torch"),
        flag_embedding_available=_module_available(UNUSED_FLAG_MODULE),
    )


def validate_runtime_capabilities(
    *,
    declared_profile: RuntimeProfile,
    embedding_provider: str,
    marker_path: Path = IMAGE_PROFILE_MARKER,
) -> RuntimeCapabilities:
    capabilities = inspect_runtime_capabilities(
        declared_profile,
        marker_path=marker_path,
    )
    profile = capabilities.effective_profile

    if profile in KNOWN_PROFILES and capabilities.flag_embedding_available:
        raise ValueError(
            "FlagEmbedding is not a supported serving dependency; remove the unused package"
        )
    if profile == "remote":
        if embedding_provider == "local":
            raise ValueError(
                "EMBEDDING_PROVIDER=local requires an APP_RUNTIME_PROFILE=local-ml "
                "backend image"
            )
        unexpected = []
        if capabilities.local_ml_available:
            unexpected.append(LOCAL_ML_MODULE)
        if capabilities.torch_available:
            unexpected.append("torch")
        if unexpected:
            raise ValueError(
                "remote runtime profile contains forbidden local ML modules: "
                + ", ".join(unexpected)
            )
    elif profile == "local-ml" and (
        not capabilities.local_ml_available or not capabilities.torch_available
    ):
        raise ValueError(
            "APP_RUNTIME_PROFILE=local-ml requires sentence-transformers and torch; "
            "rebuild with --build-arg APP_RUNTIME_PROFILE=local-ml or run "
            "uv sync --extra local-ml"
        )
    elif (
        profile == "auto"
        and embedding_provider == "local"
        and not capabilities.local_ml_available
    ):
        raise ValueError(
            "EMBEDDING_PROVIDER=local requires sentence-transformers; "
            "run uv sync --extra local-ml"
        )

    return capabilities


__all__ = [
    "IMAGE_PROFILE_MARKER",
    "RuntimeCapabilities",
    "RuntimeProfile",
    "inspect_runtime_capabilities",
    "validate_runtime_capabilities",
]
