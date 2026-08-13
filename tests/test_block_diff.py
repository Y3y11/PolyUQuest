from __future__ import annotations

from agent_rag.versioning import build_block_diff


def _block(block_id: str, content: str, path: str = "p:nth-of-type(1)") -> dict:
    return {
        "block_id": block_id,
        "content": content,
        "heading_context": "Admissions",
        "html_tag_path": path,
        "depth": 2,
        "child_index": 0,
    }


def test_diff_classifies_semantic_structure_and_membership_changes() -> None:
    old = [
        _block("same", "same"),
        _block("meta", "same metadata"),
        _block("modified", "old text"),
        _block("deleted", "gone"),
    ]
    new = [
        _block("same", "same"),
        _block("meta", "same metadata", "p:nth-of-type(2)"),
        _block("modified", "new text"),
        _block("added", "brand new"),
    ]
    plan = build_block_diff(old, new)
    assert plan.unchanged_ids == ["same"]
    assert plan.metadata_changed_ids == ["meta"]
    assert plan.modified_ids == ["modified"]
    assert plan.added_ids == ["added"]
    assert plan.deleted_ids == ["deleted"]


def test_diff_matches_relocated_blocks_deterministically() -> None:
    old = [_block("old-b", "same evidence")]
    new = [_block("new-b", "same evidence", "p:nth-of-type(2)")]
    plan = build_block_diff(old, new)
    assert [item.model_dump() for item in plan.relocated] == [
        {"old_id": "old-b", "new_id": "new-b"}
    ]
    assert plan.added_ids == []
    assert plan.deleted_ids == []


def test_page_semantic_diff_ignores_whitespace_only_changes() -> None:
    plan = build_block_diff(
        [], [], old_page={"title": "A  page"}, new_page={"title": "A page"}
    )
    assert plan.page_semantic_changed is False
