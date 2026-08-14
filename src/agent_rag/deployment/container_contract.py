"""Deterministic runtime contract for the backend container image."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EXPECTED_UID = 10001
EXPECTED_COMMANDS = (
    "agent-rag-serve",
    "agent-rag-worker",
    "agent-rag-container-check",
)
EXPECTED_MODULES = (
    "agent_rag.api.main",
    "agent_rag.workers.main",
)


def _current_uid() -> int:
    getter = getattr(os, "getuid", None)
    return int(getter()) if getter is not None else -1


def _probe_writable(path: Path) -> tuple[bool, str]:
    probe = path / ".container-contract-write-probe"
    try:
        with probe.open("x", encoding="utf-8") as handle:
            handle.write("probe")
        probe.unlink()
        return True, "write_succeeded"
    except OSError as exc:
        return False, type(exc).__name__


def evaluate_container_contract(
    *,
    app_root: Path = Path("/app"),
    home: Path = Path("/home/app"),
    expected_uid: int = EXPECTED_UID,
) -> dict[str, Any]:
    """Evaluate the image without contacting models, websites, or databases."""
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    uid = _current_uid()
    record("non_root_uid", uid == expected_uid, f"uid={uid};expected={expected_uid}")

    required_paths = (
        app_root / "src" / "agent_rag",
        app_root / "configs",
        app_root / "data",
    )
    for path in required_paths:
        record(f"path:{path}", path.exists(), "present" if path.exists() else "missing")

    for command in EXPECTED_COMMANDS:
        resolved = shutil.which(command)
        record(f"command:{command}", resolved is not None, resolved or "missing")

    for module in EXPECTED_MODULES:
        found = importlib.util.find_spec(module) is not None
        record(f"module:{module}", found, "importable" if found else "missing")

    writable_paths = (
        app_root / "data" / "runtime",
        app_root / "data" / "cache",
        home / ".cache",
    )
    for path in writable_paths:
        writable, detail = _probe_writable(path)
        record(f"writable:{path}", writable, detail)

    root_writable, root_detail = _probe_writable(app_root)
    record("app_root_read_only", not root_writable, root_detail)

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all(check["ok"] for check in checks),
        "uid": uid,
        "checks": checks,
    }


def main() -> int:
    report = evaluate_container_contract()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
