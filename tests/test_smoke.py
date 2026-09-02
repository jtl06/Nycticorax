from __future__ import annotations

import json
import unittest

from nycti.smoke import build_smoke_result


class SmokeResultTests(unittest.TestCase):
    def test_result_exposes_trace_and_additive_phases_without_private_metrics(self) -> None:
        result = build_smoke_result(
            prompt="Why did SNOW move?",
            answer="No company-specific catalyst surfaced.",
            depth="grounded",
            metrics={
                "end_to_end_ms": 1000,
                "context_fetch_ms": 100,
                "chat_llm_ms": 600,
                "tool_execution_wall_ms": 200,
                "memory_retrieval_ms": 50,
                "reply_send_ms": 0,
                "_diagnostic_agent_steps_json": json.dumps(
                    [{"state": "model", "latency_ms": 600}]
                ),
                "_secret_internal": "hidden",
            },
        )

        self.assertEqual("railway_headless_smoke", result["mode"])
        self.assertEqual("Why did SNOW move?", result["prompt"])
        self.assertEqual(600, result["steps"][0]["latency_ms"])
        self.assertNotIn("_secret_internal", result["metrics"])
        phases = result["phases"]
        self.assertEqual(1000, phases["timing_total_ms"])
        self.assertEqual(
            phases["timing_total_ms"],
            sum(
                value
                for key, value in phases.items()
                if key != "timing_total_ms"
            ),
        )


if __name__ == "__main__":
    unittest.main()
