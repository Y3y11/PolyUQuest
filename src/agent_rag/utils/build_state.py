"""build_id lifecycle for offline pipeline.

`build_state.json` tracks the current build_id and a small ring of previous ones.
A new build_id is allocated only when stage_crawl is the first selected stage;
otherwise the current_build_id is reused, so partial reruns
(`--from-stage extract` etc.) keep the same build_id and don't
falsely retire still-live objects during orphan detection.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_PIPELINE_DIR = Path(__file__).resolve().parents[3] / "data" / "pipeline"
_BUILD_STATE_PATH = _PIPELINE_DIR / "build_state.json"
_HISTORY_LIMIT = 5


def make_build_id() -> str:
    """Return a new build_id like '20260426_154301_abc12'."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{secrets.token_hex(3)[:5]}"


def load_build_state() -> dict:
    if not _BUILD_STATE_PATH.exists():
        return {}
    try:
        return json.loads(_BUILD_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_build_state(state: dict) -> None:
    _PIPELINE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".build_state_", suffix=".json", dir=str(_PIPELINE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _BUILD_STATE_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def resolve_build_id(crawl_first: bool) -> tuple[str, bool]:
    """Return (build_id, is_new).

    - crawl_first=True  → allocate new id (a fresh ingestion run).
    - crawl_first=False → reuse current_build_id from build_state.json,
                          or allocate new if the file is missing.
    """
    state = load_build_state()
    if crawl_first or not state.get("current_build_id"):
        return make_build_id(), True
    return state["current_build_id"], False


def commit_build_id(build_id: str, is_new: bool) -> None:
    """Persist build_id to build_state.json. Called after a successful pipeline run.

    For a new build_id, the previous current_build_id (if any) is pushed onto
    `previous_build_ids` (most-recent-first, capped at _HISTORY_LIMIT).
    For a reused build_id, only `last_updated` changes.
    """
    state = load_build_state()
    now = datetime.now(timezone.utc).isoformat()

    if is_new:
        prev = state.get("current_build_id")
        history = state.get("previous_build_ids", [])
        if prev and prev != build_id and prev not in history:
            history = [prev] + history
        state["previous_build_ids"] = history[:_HISTORY_LIMIT]

    state["current_build_id"] = build_id
    state["last_updated"] = now
    save_build_state(state)
