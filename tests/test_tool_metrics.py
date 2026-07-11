from __future__ import annotations

import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.loop_messages import append_tool_outcomes
from nycti.chat.run_state import AgentRun, ToolOutcome, ToolStatus


class ToolMetricAggregationTests(unittest.TestCase):
    def test_quote_success_coverage_uses_largest_batch_not_sum(self) -> None:
        run = AgentRun(messages=[])
        metrics: dict[str, int | str] = {}
        trace = AgentTrace(enabled=False)

        for call_id in ("quote-1", "quote-2"):
            append_tool_outcomes(
                run,
                [
                    ToolOutcome(
                        call_id=call_id,
                        tool_name="quote",
                        arguments='{"symbols":["NVDA","TSM","AVGO","AMD","MU"]}',
                        status=ToolStatus.OK,
                        content="Five successful quotes.",
                        metrics={"stock_quote_success_symbol_count": 5},
                    )
                ],
                metrics=metrics,
                trace=trace,
            )

        self.assertEqual(5, metrics["stock_quote_success_symbol_count"])
        self.assertEqual(2, metrics["tool_call_count"])


if __name__ == "__main__":
    unittest.main()
