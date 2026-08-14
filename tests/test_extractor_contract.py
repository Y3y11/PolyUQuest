from __future__ import annotations

from agent_rag.kg.extractor import extract_from_page


class _LLM:
    def __init__(self, raw: str):
        self.raw = raw

    def chat(self, **_kwargs):
        return self.raw


def test_page_extraction_accepts_open_domain_types(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_rag.kg.extractor.get_cached", lambda *_args: None)
    monkeypatch.setattr("agent_rag.kg.extractor.set_cached", lambda *_args: None)
    raw = """{
      "entities": [{
        "name":"Widget X", "type":"PRODUCT",
        "description":"A widget", "source_block_refs":["B1"]
      }],
      "relations": []
    }"""
    result = extract_from_page(
        "https://example.org/product",
        [{"block_id": "block-1", "content": "Widget X is available."}],
        llm=_LLM(raw),
        strict=True,
    )
    assert result.entities[0].type == "PRODUCT"
    assert result.entities[0].source_block_refs == ["block-1"]


def test_strict_extraction_raises_on_invalid_nonempty_payload(monkeypatch) -> None:
    monkeypatch.setattr("agent_rag.kg.extractor.get_cached", lambda *_args: None)
    monkeypatch.setattr("agent_rag.kg.extractor.set_cached", lambda *_args: None)
    raw = '{"entities":[{"name":"Broken"}],"relations":[]}'
    try:
        extract_from_page(
            "https://example.org/product",
            [{"block_id": "block-1", "content": "Broken"}],
            llm=_LLM(raw),
            strict=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("strict extraction must reject invalid non-empty output")


def test_strict_extraction_rejects_missing_provenance(monkeypatch) -> None:
    monkeypatch.setattr("agent_rag.kg.extractor.get_cached", lambda *_args: None)
    monkeypatch.setattr("agent_rag.kg.extractor.set_cached", lambda *_args: None)
    raw = """{
      "entities": [{
        "name":"Widget X", "type":"PRODUCT", "description":"Widget",
        "source_block_refs":["UNKNOWN"]
      }], "relations": []
    }"""
    try:
        extract_from_page(
            "https://example.org/product",
            [{"block_id": "block-1", "content": "Widget X"}],
            llm=_LLM(raw),
            strict=True,
        )
    except ValueError as exc:
        assert "provenance" in str(exc)
    else:
        raise AssertionError("strict extraction must require valid block refs")
