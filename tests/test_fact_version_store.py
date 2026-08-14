from __future__ import annotations

from agent_rag.knowledge import FactDelta, FactVersionStore


def _fact(blocks: list[str], description: str = "A requires B") -> FactDelta:
    return FactDelta(
        fact_key="fact-1",
        source_id="a",
        source_name="A",
        target_id="b",
        target_name="B",
        relation_type="requires",
        description=description,
        source_block_ids=blocks,
    )


def test_fact_history_is_idempotent_and_bitemporal(tmp_path) -> None:
    store = FactVersionStore(tmp_path / "facts.db")
    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v1",
        valid_at="2026-08-14T00:00:00Z",
        current_facts=[_fact(["b1"])],
        retired_fact_keys=[],
    ) == (1, 0, 0)
    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v1",
        valid_at="2026-08-14T00:00:00Z",
        current_facts=[_fact(["b1"])],
        retired_fact_keys=[],
    ) == (1, 0, 0)

    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v2",
        valid_at="2026-08-15T00:00:00Z",
        current_facts=[_fact(["b2"], "A now requires B")],
        retired_fact_keys=[],
    ) == (0, 1, 0)
    history = store.history("fact-1")
    assert len(history) == 2
    assert history[0].status == "active"
    assert history[1].status == "retired"
    assert history[1].valid_to == "2026-08-15T00:00:00Z"

    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v3",
        valid_at="2026-08-16T00:00:00Z",
        current_facts=[],
        retired_fact_keys=["fact-1"],
    ) == (0, 0, 1)
    assert store.stats() == {"active": 0, "retired": 2, "total_versions": 2}


def test_existing_graph_fact_is_bootstrapped_before_retirement(tmp_path) -> None:
    store = FactVersionStore(tmp_path / "facts.db")
    old = _fact(["old-block"], "Legacy fact")
    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v1",
        valid_at="2026-08-14T00:00:00Z",
        current_facts=[],
        retired_fact_keys=["fact-1"],
        previous_facts=[old],
    ) == (0, 0, 1)
    history = store.history("fact-1")
    assert len(history) == 1
    assert history[0].status == "retired"
    assert history[0].introduced_version_id == "bootstrap-current-graph"
    assert history[0].retired_version_id == "v1"

    # Event ledger makes retry metrics idempotent as well as state idempotent.
    assert store.apply(
        source_url="https://example.org/docs",
        version_id="v1",
        valid_at="2026-08-14T00:00:00Z",
        current_facts=[],
        retired_fact_keys=["fact-1"],
        previous_facts=[old],
    ) == (0, 0, 1)
