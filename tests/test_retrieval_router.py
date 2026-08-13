from __future__ import annotations

import unittest

from agent_rag.retrieval.router import route_query


class _UnexpectedLLM:
    def chat(self, **_kwargs):
        raise AssertionError("procedural query should not call the LLM router")


class RetrievalRouterTests(unittest.TestCase):
    def test_chinese_procedure_query_uses_direct_rule(self) -> None:
        decision = route_query(
            "支付服务 v4 应该怎么部署",
            llm=_UnexpectedLLM(),
        )
        self.assertEqual(decision["mode"], "mode_a")
        self.assertEqual(decision["source"], "heuristic")
        self.assertEqual(decision["confidence"], 1.0)

    def test_english_procedure_query_uses_direct_rule(self) -> None:
        decision = route_query(
            "How do I configure Payment Service v4?",
            llm=_UnexpectedLLM(),
        )
        self.assertEqual(decision["mode"], "mode_a")
        self.assertEqual(decision["source"], "heuristic")


if __name__ == "__main__":
    unittest.main()
