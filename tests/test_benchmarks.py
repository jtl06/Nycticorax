import unittest

from nycti.benchmarks import (
    CONTEXT_BENCHMARK_HISTORY,
    CONTEXT_BENCHMARK_PROMPT,
    EARNINGS_BENCHMARK_PROMPT,
    SEMI_BLOODBATH_BENCHMARK_PROMPT,
    SPACEX_PRICE_BENCHMARK_PROMPT,
    build_context_benchmark_tool_runner,
    build_earnings_benchmark_tool_runner,
    format_context_benchmark_score,
    format_current_price_benchmark_score,
    format_earnings_benchmark_score,
    format_sector_quote_benchmark_score,
    score_context_benchmark,
    score_current_price_benchmark,
    score_earnings_benchmark,
    score_sector_quote_benchmark,
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


class EarningsBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_requires_all_scored_fields_and_actuals(self) -> None:
        self.assertLessEqual(len(EARNINGS_BENCHMARK_PROMPT), 220)
        self.assertIn("NVIDIA", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("AMD", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("report period/date", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("actual revenue", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("adjusted EPS", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("revenue guidance", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("source links", EARNINGS_BENCHMARK_PROMPT)
        self.assertNotIn("site:", EARNINGS_BENCHMARK_PROMPT)
        self.assertNotIn("Use tools", EARNINGS_BENCHMARK_PROMPT)

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

    async def test_fixture_runner_returns_pinned_earnings_evidence(self) -> None:
        runner = build_earnings_benchmark_tool_runner()
        call = type(
            "ToolCall",
            (),
            {"id": "call_1", "name": "web", "arguments": '{"query":"latest earnings"}'},
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

        self.assertEqual(ToolStatus.OK, outcomes[0].status)
        self.assertIn("$81.615 billion", outcomes[0].content)
        self.assertIn("$10.253 billion", outcomes[0].content)
        self.assertEqual(1, outcomes[0].metrics["web_search_query_count"])


class ContextBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_is_a_natural_user_request(self) -> None:
        self.assertLessEqual(len(CONTEXT_BENCHMARK_PROMPT), 120)
        self.assertIn("final deployment plan", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("owners", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("open question", CONTEXT_BENCHMARK_PROMPT)
        self.assertIn("go/no-go deadline", CONTEXT_BENCHMARK_PROMPT)
        self.assertNotIn("channel_ctx", CONTEXT_BENCHMARK_PROMPT)
        self.assertNotIn("use tools", CONTEXT_BENCHMARK_PROMPT.casefold())

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


class CurrentPriceBenchmarkTests(unittest.TestCase):
    def test_prompt_targets_short_discord_spacex_price_failure(self) -> None:
        self.assertEqual("What's the current price of SpaceX?", SPACEX_PRICE_BENCHMARK_PROMPT)
        self.assertNotIn("Use tools", SPACEX_PRICE_BENCHMARK_PROMPT)
        self.assertNotIn("private", SPACEX_PRICE_BENCHMARK_PROMPT.lower())
        self.assertNotIn("ticker", SPACEX_PRICE_BENCHMARK_PROMPT.lower())

    def test_stale_private_answer_fails_without_tool_use(self) -> None:
        answer = (
            "SpaceX is private - no ticker, no public price. "
            "I don't have live trading data for it."
        )
        score = score_current_price_benchmark(answer, {})

        self.assertFalse(score.used_tool)
        self.assertFalse(score.used_quote)
        self.assertFalse(score.avoids_stale_private_claim)
        self.assertIn("tool used", score.failed)
        self.assertIn("quote used", score.failed)
        self.assertIn("stale private/no ticker claim avoided", score.failed)

    def test_web_only_market_cap_answer_does_not_pass_current_price(self) -> None:
        answer = (
            "SpaceX went public recently and currently trades around a $2.5-2.75 trillion "
            "market cap. Shares have been surging after crossing $2.5 trillion on Monday."
        )
        metrics = {
            "agent_tool_call_count": 1,
            "web_search_query_count": 1,
        }

        score = score_current_price_benchmark(answer, metrics)

        self.assertFalse(score.includes_price_or_grounded_uncertainty)
        self.assertFalse(score.used_quote)
        self.assertIn("quote used", score.failed)
        self.assertIn("price or grounded uncertainty", score.failed)

    def test_grounded_spcx_answer_passes(self) -> None:
        answer = (
            "SPCX is currently showing as the public SpaceX ticker in the fresh market data I checked. "
            "It last traded around $205.10 today; ignore similarly named crypto/token pages."
        )
        metrics = {
            "agent_tool_call_count": 2,
            "web_search_query_count": 1,
            "stock_quote_count": 1,
            "agent_model_turn_count": 2,
            "chat_total_tokens": 900,
            "end_to_end_ms": 1200,
        }
        score = score_current_price_benchmark(answer, metrics)
        rendered = format_current_price_benchmark_score(score, metrics)

        self.assertEqual(6, score.points)
        self.assertEqual((), score.failed)
        self.assertIn("current_price_benchmark", rendered)
        self.assertIn("score=6/6 failed=none", rendered)
        self.assertIn("web_queries=1 quotes=1", rendered)


class SectorQuoteBenchmarkTests(unittest.TestCase):
    def test_prompt_is_short_discord_style_request(self) -> None:
        self.assertEqual(
            "hows the great semi bloodbath today, report on all semi companies > 100b",
            SEMI_BLOODBATH_BENCHMARK_PROMPT,
        )
        self.assertLessEqual(len(SEMI_BLOODBATH_BENCHMARK_PROMPT), 80)
        self.assertNotIn("Use quote", SEMI_BLOODBATH_BENCHMARK_PROMPT)
        self.assertNotIn("tool", SEMI_BLOODBATH_BENCHMARK_PROMPT.lower())

    def test_web_snippet_fallback_fails(self) -> None:
        answer = (
            "I found web sources, but couldn't synthesize a clean answer. Unsynthesized snippets for "
            "semiconductor companies market cap over 100 billion USD 2026: Yahoo Finance semiconductors..."
        )

        score = score_sector_quote_benchmark(answer, {"web_search_query_count": 1})

        self.assertFalse(score.used_quote)
        self.assertFalse(score.enough_quote_symbols)
        self.assertFalse(score.mentions_mu)
        self.assertFalse(score.avoids_unsynthesized_web_fallback)
        self.assertIn("quote used", score.failed)
        self.assertIn("web fallback avoided", score.failed)

    def test_missing_mu_and_bad_prices_fail(self) -> None:
        answer = (
            "NVDA -1.2%, TSM -7%, AVGO -1.7%, AMD $540.88 -6.9%, INTC flat, "
            "QCOM -1.5%, AMAT -10%, LRCX -9.7%, TXN +0.1%, KLAC -11.8%."
        )
        metrics = {
            "agent_tool_call_count": 1,
            "stock_quote_count": 1,
            "stock_quote_symbol_count": 10,
        }

        score = score_sector_quote_benchmark(answer, metrics)

        self.assertFalse(score.mentions_mu)
        self.assertFalse(score.avoids_obvious_bad_prices)
        self.assertIn("MU included", score.failed)
        self.assertIn("obvious bad prices avoided", score.failed)

    def test_quote_based_sector_answer_passes(self) -> None:
        answer = (
            "Semis >$100B today: NVDA -1.2%, TSM -7.0%, AVGO -1.7%, AMD -6.9%, ASML -3.1%, "
            "QCOM -1.5%, TXN +0.1%, AMAT -10.0%, MU -10.6%, INTC -0.4%, KLAC -11.8%, LRCX -9.7%."
        )
        metrics = {
            "agent_tool_call_count": 2,
            "stock_quote_count": 2,
            "stock_quote_symbol_count": 12,
            "agent_model_turn_count": 3,
            "chat_total_tokens": 1500,
            "end_to_end_ms": 5000,
        }

        score = score_sector_quote_benchmark(answer, metrics)
        rendered = format_sector_quote_benchmark_score(score, metrics)

        self.assertEqual(6, score.points)
        self.assertEqual((), score.failed)
        self.assertIn("sector_quote_benchmark", rendered)
        self.assertIn("score=6/6 failed=none", rendered)
        self.assertIn("quotes=2 quote_symbols=12", rendered)


if __name__ == "__main__":
    unittest.main()
