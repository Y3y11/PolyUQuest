from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_rag.telemetry.recorder import (
    TelemetryRecorder,
    record_current_llm_usage,
)
from agent_rag.telemetry.store import TelemetryStore


class TelemetryTests(unittest.TestCase):
    def test_per_run_context_separates_usage_and_billing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
            recorder = TelemetryRecorder(store)
            recorder.start_run("run-a", "agent_query", query="secret question A")
            recorder.start_run("run-b", "agent_query", query="secret question B")
            with recorder.bind("run-a"):
                recorder.record_llm(
                    stage="router",
                    provider="deepseek",
                    model="v4-flash",
                    input_tokens=10,
                    output_tokens=2,
                    cache_hit=False,
                    duration_ms=12,
                )
            with recorder.bind("run-b"):
                recorder.record_llm(
                    stage="answer",
                    provider="deepseek",
                    model="v4-flash",
                    input_tokens=30,
                    output_tokens=8,
                    cache_hit=True,
                    duration_ms=0,
                )

            run_a = store.get("run-a")
            run_b = store.get("run-b")
            assert run_a is not None and run_b is not None
            self.assertEqual(run_a.run.logical_input_tokens, 10)
            self.assertEqual(run_a.run.billable_input_tokens, 10)
            self.assertEqual(run_b.run.logical_input_tokens, 30)
            self.assertEqual(run_b.run.billable_input_tokens, 0)
            self.assertEqual(run_b.run.cache_hits, 1)
            self.assertNotIn("secret question", run_a.run.model_dump_json())

    def test_store_computes_nearest_rank_percentiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
            recorder = TelemetryRecorder(store)
            for index, duration in enumerate([10, 20, 30, 40]):
                run_id = f"run-{index}"
                recorder.start_run(run_id, "agent_query")
                store.finish(
                    run_id,
                    status="completed",
                    completed_at="2026-08-14T00:00:00+00:00",
                    duration_ms=duration,
                )
            stats = store.stats(hours=24)
            self.assertEqual(stats["p50_duration_ms"], 20)
            self.assertEqual(stats["p95_duration_ms"], 40)

    def test_recorder_failure_does_not_break_business_flow(self) -> None:
        class BrokenStore:
            def start(self, _run):
                raise OSError("disk unavailable")

        recorder = TelemetryRecorder(BrokenStore())  # type: ignore[arg-type]
        recorder.start_run("run", "agent_query", query="hello")
        self.assertEqual(recorder.dropped_writes, 1)


class TelemetryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_contextvar_attribution_survives_task_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TelemetryStore(Path(temp_dir) / "telemetry.sqlite3")
            recorder = TelemetryRecorder(store)
            recorder.start_run("run-a", "agent_query")
            recorder.start_run("run-b", "agent_query")

            async def record(run_id: str, tokens: int) -> None:
                with recorder.bind(run_id):
                    await asyncio.sleep(0)
                    record_current_llm_usage(
                        stage="answer",
                        provider="deepseek",
                        model="v4-flash",
                        input_tokens=tokens,
                        output_tokens=1,
                        cache_hit=False,
                        duration_ms=1,
                    )

            await asyncio.gather(record("run-a", 11), record("run-b", 29))
            run_a = store.get("run-a")
            run_b = store.get("run-b")
            assert run_a is not None and run_b is not None
            self.assertEqual(run_a.run.logical_input_tokens, 11)
            self.assertEqual(run_b.run.logical_input_tokens, 29)


if __name__ == "__main__":
    unittest.main()
