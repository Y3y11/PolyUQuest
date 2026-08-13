"""Deterministic block matching for incremental page publication."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def semantic_fingerprint(block: dict[str, Any]) -> str:
    return _digest(
        {
            "content": _normalise(block.get("content")),
            "heading_context": _normalise(block.get("heading_context")),
        }
    )


def structure_fingerprint(block: dict[str, Any]) -> str:
    return _digest(
        {
            "html_tag_path": _normalise(block.get("html_tag_path")),
            "depth": int(block.get("depth") or 0),
            "parent_block_id": _normalise(block.get("parent_block_id")),
            "child_index": int(block.get("child_index") or 0),
        }
    )


def page_semantic_fingerprint(page: dict[str, Any] | None) -> str:
    page = page or {}
    return _digest(
        {
            "title": _normalise(page.get("title")),
            "meta_description": _normalise(page.get("meta_description")),
        }
    )


class RelocatedBlock(BaseModel):
    old_id: str
    new_id: str


class BlockDiffPlan(BaseModel):
    old_count: int = 0
    new_count: int = 0
    unchanged_ids: list[str] = Field(default_factory=list)
    metadata_changed_ids: list[str] = Field(default_factory=list)
    modified_ids: list[str] = Field(default_factory=list)
    added_ids: list[str] = Field(default_factory=list)
    deleted_ids: list[str] = Field(default_factory=list)
    relocated: list[RelocatedBlock] = Field(default_factory=list)
    page_semantic_changed: bool = False

    @property
    def write_ids(self) -> set[str]:
        return {
            *self.metadata_changed_ids,
            *self.modified_ids,
            *self.added_ids,
            *(item.new_id for item in self.relocated),
        }


def build_block_diff(
    old_blocks: list[dict[str, Any]],
    new_blocks: list[dict[str, Any]],
    *,
    old_page: dict[str, Any] | None = None,
    new_page: dict[str, Any] | None = None,
) -> BlockDiffPlan:
    """Classify a page snapshot without fuzzy or model-dependent matching."""
    old_by_id = {str(item["block_id"]): item for item in old_blocks}
    new_by_id = {str(item["block_id"]): item for item in new_blocks}
    common = sorted(old_by_id.keys() & new_by_id.keys())

    unchanged: list[str] = []
    metadata_changed: list[str] = []
    modified: list[str] = []
    for block_id in common:
        old, new = old_by_id[block_id], new_by_id[block_id]
        if semantic_fingerprint(old) != semantic_fingerprint(new):
            modified.append(block_id)
        elif structure_fingerprint(old) != structure_fingerprint(new):
            metadata_changed.append(block_id)
        else:
            unchanged.append(block_id)

    old_remaining = sorted(old_by_id.keys() - new_by_id.keys())
    new_remaining = sorted(new_by_id.keys() - old_by_id.keys())
    old_by_fingerprint: dict[str, list[str]] = defaultdict(list)
    new_by_fingerprint: dict[str, list[str]] = defaultdict(list)
    for block_id in old_remaining:
        old_by_fingerprint[semantic_fingerprint(old_by_id[block_id])].append(block_id)
    for block_id in new_remaining:
        new_by_fingerprint[semantic_fingerprint(new_by_id[block_id])].append(block_id)

    relocated: list[RelocatedBlock] = []
    relocated_old: set[str] = set()
    relocated_new: set[str] = set()
    for fingerprint in sorted(old_by_fingerprint.keys() & new_by_fingerprint.keys()):
        for old_id, new_id in zip(
            sorted(old_by_fingerprint[fingerprint]),
            sorted(new_by_fingerprint[fingerprint]),
            strict=False,
        ):
            relocated.append(RelocatedBlock(old_id=old_id, new_id=new_id))
            relocated_old.add(old_id)
            relocated_new.add(new_id)

    return BlockDiffPlan(
        old_count=len(old_blocks),
        new_count=len(new_blocks),
        unchanged_ids=unchanged,
        metadata_changed_ids=metadata_changed,
        modified_ids=modified,
        added_ids=sorted(set(new_remaining) - relocated_new),
        deleted_ids=sorted(set(old_remaining) - relocated_old),
        relocated=relocated,
        page_semantic_changed=(
            page_semantic_fingerprint(old_page) != page_semantic_fingerprint(new_page)
        ),
    )
