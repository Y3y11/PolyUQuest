from __future__ import annotations

import unittest

from agent_rag.agent.frontier import FrontierSelector
from agent_rag.tools.schemas import FrontierSeed, QueryProfile


class _FakeLLM:
    def __init__(self, response: str):
        self.response = response

    def chat(self, **_kwargs):
        return self.response

    def close(self):
        pass


class FrontierSelectorTests(unittest.TestCase):
    def test_clear_score_winner_does_not_call_llm(self) -> None:
        def fail_factory():
            raise AssertionError("LLM must not be called for a clear winner")

        selector = FrontierSelector(llm_factory=fail_factory)
        candidates = [
            FrontierSeed(url="https://www.polyu.edu.hk/a", score=0.9),
            FrontierSeed(url="https://www.polyu.edu.hk/b", score=0.4),
        ]
        selected, source, _ = selector.select(
            "query", candidates, QueryProfile(query="query"), ["claim"]
        )
        self.assertEqual(selected.url, "https://www.polyu.edu.hk/a")
        self.assertEqual(source, "rule")

    def test_llm_can_choose_only_from_shortlist(self) -> None:
        selector = FrontierSelector(
            llm_factory=lambda: _FakeLLM('{"index":1,"reason":"better hub"}')
        )
        candidates = [
            FrontierSeed(url="https://www.polyu.edu.hk/a", score=0.9),
            FrontierSeed(url="https://www.polyu.edu.hk/b", score=0.85),
        ]
        selected, source, reason = selector.select(
            "query", candidates, QueryProfile(query="query"), ["claim"]
        )
        self.assertEqual(selected.url, "https://www.polyu.edu.hk/b")
        self.assertEqual(source, "llm")
        self.assertEqual(reason, "better hub")

    def test_invalid_llm_index_falls_back_to_top_score(self) -> None:
        selector = FrontierSelector(llm_factory=lambda: _FakeLLM('{"index":99}'))
        candidates = [
            FrontierSeed(url="https://www.polyu.edu.hk/a", score=0.9),
            FrontierSeed(url="https://www.polyu.edu.hk/b", score=0.85),
        ]
        selected, source, _ = selector.select(
            "query", candidates, QueryProfile(query="query"), ["claim"]
        )
        self.assertEqual(selected.url, "https://www.polyu.edu.hk/a")
        self.assertEqual(source, "fallback")


if __name__ == "__main__":
    unittest.main()
