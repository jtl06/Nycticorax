import unittest

from nycti.benchmarks import (
    CONTEXT_BENCHMARK_HISTORY,
    CONTEXT_BENCHMARK_PROMPT,
    EARNINGS_BENCHMARK_PROMPT,
    build_context_benchmark_tool_runner,
    format_context_benchmark_score,
    format_earnings_benchmark_score,
    score_context_benchmark,
    score_earnings_benchmark,
)
from nycti.chat.run_state import AgentPermissions, ToolStatus


COMPLETE_ANSWER = """
NVIDIA reported Q1 FY2027 results on May 20, 2026. Actual revenue was $81.6 billion and
adjusted diluted EPS was $1.87. Next-quarter revenue guidance is $91 billion, plus or minus 2%.
Source: https://investor.nvidia.com/news/results-q1

AMD reported Q1 2026 results on May 5, 2026. Actual revenue was $10.3 billion and
non-GAAP diluted EPS was $1.37. Next-quarter guidance: $11.2 billion, plus or minus $300 million.
Source: https://ir.amd.com/news-events/results-q2
"""

COMPLETE_CONTEXT_ANSWER = """
Final plan: deploy Thursday, June 18, 2026 at 16:00 UTC using a 10% canary for 30 minutes,
then complete the rollout if healthy.
- Marcus owns the rollback runbook and rollback drill, due June 16 at 18:00 UTC.
- Elena owns the alert dashboard and paging checks, due June 17 at 12:00 UTC.
- Unresolved: whether mobile clients need a forced refresh after deployment.
- Go/no-go deadline: June 17 at 15:00 UTC.
"""


class EarningsBenchmarkTests(unittest.TestCase):
    def test_prompt_requires_all_scored_fields_and_actuals(self) -> None:
        self.assertIn("report date", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("actual revenue", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("adjusted/non-GAAP diluted EPS", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("revenue guidance", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("official investor-relations or SEC source URL", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("Do not substitute analyst estimates", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("Never construct or guess an investor-relations URL", EARNINGS_BENCHMARK_PROMPT)

    def test_complete_grounded_answer_gets_full_score(self) -> None:
        score = score_earnings_benchmark(COMPLETE_ANSWER)

        self.assertEqual(10, score.completeness_points)
        self.assertEqual(10, score.correctness_checks)
        self.assertEqual((), score.missing)
        self.assertEqual((), score.incorrect)

    def test_score_reports_missing_company_fields(self) -> None:
        score = score_earnings_benchmark(
            "NVIDIA reported Q1 fiscal results on May 20, 2026 with revenue of $44 billion."
        )

        self.assertLess(score.completeness_points, 10)
        self.assertIn("NVIDIA adjusted EPS", score.missing)
        self.assertIn("AMD actual revenue", score.missing)

    def test_unavailable_guidance_does_not_get_credit_from_nearby_actual_revenue(self) -> None:
        answer = COMPLETE_ANSWER.replace(
            "Next-quarter revenue guidance is $91 billion, plus or minus 2%.",
            "Next-quarter guidance: Specific revenue outlook was not extracted.",
        ).replace(
            "Next-quarter guidance: $11.2 billion, plus or minus $300 million.",
            "Next-quarter guidance: Specific revenue outlook was unavailable.",
        )

        score = score_earnings_benchmark(answer)

        self.assertIn("NVIDIA revenue guidance", score.missing)
        self.assertIn("AMD revenue guidance", score.missing)
        self.assertEqual(8, score.completeness_points)

    def test_wrong_values_are_present_but_fail_correctness(self) -> None:
        answer = COMPLETE_ANSWER.replace("$81.6 billion", "$71.6 billion").replace(
            "$1.37",
            "$1.17",
        )

        score = score_earnings_benchmark(answer)

        self.assertEqual(10, score.completeness_points)
        self.assertEqual(8, score.correctness_checks)
        self.assertIn("NVIDIA actual revenue", score.incorrect)
        self.assertIn("AMD adjusted EPS", score.incorrect)

    def test_formatter_includes_quality_and_runtime_metrics(self) -> None:
        rendered = format_earnings_benchmark_score(
            score_earnings_benchmark(COMPLETE_ANSWER),
            {
                "agent_model_turn_count": 2,
                "agent_tool_call_count": 1,
                "chat_total_tokens": 1234,
                "end_to_end_ms": 4567,
            },
        )

        self.assertIn("completeness=10/10", rendered)
        self.assertIn("correctness_checks=10/10", rendered)
        self.assertIn("incorrect=none", rendered)
        self.assertIn("turns=2 tools=1 retries=0 tokens=1234 latency_ms=4567", rendered)


class ContextBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_requires_context_tool_and_scored_fields(self) -> None:
        self.assertIn("`channel_ctx`", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("final deployment plan", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("Marcus's task and due date", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("Elena's task and due date", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("unresolved mobile-client question", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("go/no-go deadline", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("Later decisions supersede earlier proposals", CONTEXT_BENCHMARK_PROMPT)

    async def test_fixture_runner_returns_seeded_history_and_metrics(self) -> None:
        runner = build_context_benchmark_tool_runner()
        call = type(
            "ToolCall",
            (),
            {"id": "call_1", "name": "channel_ctx", "arguments": '{"mode":"raw"}'},
        )()

        outcomes = await runner.run(
            [call],
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="benchmark",
            step_index=1,
        )

        self.assertEqual(1, len(outcomes))
        self.assertEqual(ToolStatus.OK, outcomes[0].status)
        self.assertEqual(CONTEXT_BENCHMARK_HISTORY, outcomes[0].content)
        self.assertEqual(1, outcomes[0].metrics["channel_context_fetch_count"])

    def test_complete_context_answer_gets_full_score(self) -> None:
        score = score_context_benchmark(
            COMPLETE_CONTEXT_ANSWER,
            {"channel_context_fetch_count": 1},
        )

        self.assertEqual(8, score.points)
        self.assertEqual((), score.failed)

    def test_score_rejects_missing_tool_use_and_superseded_plan(self) -> None:
        answer = (
            COMPLETE_CONTEXT_ANSWER
            + "\nThe final plan was also described as a Friday, June 19 blue-green deployment."
        )
        score = score_context_benchmark(
            answer,
            {"web_search_query_count": 2},
        )

        self.assertFalse(score.used_channel_context)
        self.assertFalse(score.avoided_web_search)
        self.assertFalse(score.avoided_superseded_plan)
        self.assertIn("channel_ctx used", score.failed)
        self.assertIn("external research avoided", score.failed)
        self.assertIn("superseded plan omitted", score.failed)

    def test_formatter_includes_score_and_runtime_metrics(self) -> None:
        metrics: dict[str, int | str] = {
            "channel_context_fetch_count": 1,
            "agent_model_turn_count": 2,
            "agent_tool_call_count": 1,
            "chat_total_tokens": 900,
            "end_to_end_ms": 1200,
        }
        rendered = format_context_benchmark_score(
            score_context_benchmark(COMPLETE_CONTEXT_ANSWER, metrics),
            metrics,
        )

        self.assertIn("score=8/8 failed=none", rendered)
        self.assertIn(
            "turns=2 tools=1 ctx_calls=1 web_queries=0 tokens=900 latency_ms=1200",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
