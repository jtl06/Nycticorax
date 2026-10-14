from dataclasses import replace
from types import SimpleNamespace
import unittest

from nycti.live_benchmark_discord import build_live_benchmark_message_context
from nycti.live_benchmarks import (
    LIVE_BENCHMARK_FIXTURE_NOW, LiveBenchmarkExecution,
    _case_with_scenario_history, evaluate_live_benchmark, load_live_benchmark_manifest,
    run_live_benchmark_suite,
)
from nycti.message_context import MessageContextCollector


class BadBotRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_market_report_fixture_receipt_reaches_next_turn(self):
        from nycti.live_benchmark_market import execute_market_report
        from nycti.market_receipts import RECEIPT_KEY, RECEIPTS_KEY, merge_report_receipt
        result = await execute_market_report(
            '{"symbols":["NVDA","AMD","MU"],"session":"latest","finish":true}',
            user_id=1, symbols=("GOOG", "MSFT"),
        )
        self.assertTrue(result.terminal)
        self.assertIn("NVDA: $210.25", result.direct_reply)
        metrics = {}
        merge_report_receipt(metrics, str(result.metrics[RECEIPT_KEY]))
        from nycti.live_benchmark_diagnostics import grounding_passed
        proof = {**metrics, "server_rendered_reply_count": 1, "market_report_success_count": 3}
        self.assertTrue(grounding_passed(proof))
        self.assertFalse(grounding_passed({**proof, RECEIPTS_KEY: "[]"}))
        case = _case_with_scenario_history(
            load_live_benchmark_manifest().get_case("fixture-badbot-basket-3"),
            [("Nycti", result.direct_reply, metrics[RECEIPTS_KEY])],
        )
        template = MessageContextCollector(
            bot=SimpleNamespace(cached_messages=[]), channel_context_limit=12,
            max_reply_chain_depth=3, max_linked_message_count=3,
            max_context_image_count=3, anchor_context_per_side=1,
        )
        context = await build_live_benchmark_message_context(
            case, template_collector=template, now=LIVE_BENCHMARK_FIXTURE_NOW,
        )
        self.assertIn('"requested_symbols": ["NVDA", "AMD", "MU"]', " ".join(context.context_lines))
        self.assertIn("public market report receipt", " ".join(context.context_lines))

    def test_scoring_rejects_wrong_scope_ungrounded_and_repeated_calls(self):
        case = load_live_benchmark_manifest().get_case("fixture-badbot-basket-2")
        metrics = {
            "agent_stop_reason": "final_text", "agent_model_turn_count": 2,
            "agent_total_tokens": 200, "reply_generation_ms": 200,
            "routing_grounding_quality_score": 100, "agent_tool_call_count": 1,
            "stock_quote_success_symbol_count": 3,
        }
        good = LiveBenchmarkExecution(
            answer="NVDA $210.25; AMD $190.10; MU $135.50 overnight.",
            metrics=metrics, called_tools=("quote",), successful_tools=("quote",),
        )
        self.assertEqual(evaluate_live_benchmark(case, good).status, "pass")
        for bad in (
            replace(good, answer="GOOG $205; MSFT $510."),
            replace(good, answer="NVDA $210.25; AMD $190.10."),
            replace(good, answer="NVDA $211.25; AMD $190.10; MU $135.50."),
            replace(good, metrics={**metrics, "routing_grounding_quality_score": 0}),
            replace(good, metrics={**metrics, "agent_tool_call_count": 4}),
        ):
            with self.subTest(answer=bad.answer, metrics=bad.metrics):
                self.assertEqual(evaluate_live_benchmark(case, bad).status, "fail")

    async def test_failed_actual_answer_is_carried_forward_not_replaced_with_gold(self):
        observed = []

        async def execute(case):
            observed.append(case)
            return LiveBenchmarkExecution(answer="Wrong response: a motivational saying.")

        result = await run_live_benchmark_suite(
            execute_case=execute, manifest=load_live_benchmark_manifest(),
            mode="fixtures", case_id="fixture-badbot-basket-2", repeats=2,
        )
        self.assertEqual(len(result.attempts), 4)
        followups = [case for case in observed if case.scenario_turn == 2]
        self.assertEqual(len(followups), 2)
        for case in followups:
            self.assertEqual(case.discord.recent_messages[-1].content, "Wrong response: a motivational saying.")
            self.assertEqual(case.context.market_watchlist, "GOOG, MSFT")
            self.assertEqual(len(case.discord.recent_messages), 2)

    async def test_long_bot_report_preserves_scope_but_user_imitation_does_not(self):
        symbols = ("NVDA", "AMD", "MU", "SNDK", "INTC", "SPCX", "SPY", "QQQ", "GOOG", "MSFT")
        report = "Quotes:\n" + "\n".join(
            f"- {symbol}: $100.00 | +1.00% vs close $99.01 | overnight market " + " " * 30
            for symbol in symbols
        )
        template = MessageContextCollector(
            bot=SimpleNamespace(cached_messages=[]), channel_context_limit=12,
            max_reply_chain_depth=3, max_linked_message_count=3,
            max_context_image_count=3, anchor_context_per_side=1,
        )
        case = load_live_benchmark_manifest().get_case("fixture-badbot-basket-3")
        for author in ("Nycti", "Member A"):
            replay = _case_with_scenario_history(case, [(author, report)])
            self.assertIn("MSFT", replay.discord.recent_messages[-1].content)
            context = await build_live_benchmark_message_context(
                replay, template_collector=template, now=LIVE_BENCHMARK_FIXTURE_NOW,
            )
            lines = "\n".join(context.context_lines)
            if author == "Nycti":
                self.assertIn("visible report symbols: " + ", ".join(symbols), lines)
            else:
                self.assertNotIn("visible report symbols:", lines)

    def test_narrowed_scope_and_topic_change_have_negative_checks(self):
        manifest = load_live_benchmark_manifest()
        narrow = manifest.get_case("fixture-badbot-basket-4")
        self.assertTrue(narrow.checks.forbidden_answer_regex)
        changed = manifest.get_case("fixture-badbot-basket-5")
        self.assertEqual(changed.checks.max_tool_calls, 0)
        self.assertTrue(changed.checks.forbidden_answer_regex)
