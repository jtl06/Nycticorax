from types import SimpleNamespace
import unittest

from nycti.chat.context import format_market_watchlist_block
from nycti.chat.orchestrator_support import format_available_tool_guidance
from nycti.live_benchmarks import LiveBenchmarkExecution, evaluate_live_benchmark, load_live_benchmark_manifest


class WatchlistQuoteContextTests(unittest.TestCase):
    def test_benchmark_rejects_saying_and_incomplete_basket(self):
        case = load_live_benchmark_manifest().get_case("fixture-watchlist-session-quote")
        metrics = {"agent_stop_reason": "final_text", "agent_model_turn_count": 1,
                   "agent_total_tokens": 100, "reply_generation_ms": 100,
                   "stock_quote_success_symbol_count": 10, "routing_grounding_quality_score": 100,
                   "agent_tool_call_count": 1}
        for answer in ("Don't make permanent decisions from temporary impatience.",
                       "GOOG 1, SNDK 2, MU 3, AMD 4, INTC 5, MSFT 6 overnight"):
            result = evaluate_live_benchmark(case, LiveBenchmarkExecution(
                answer=answer, metrics=metrics, called_tools=("quote",), successful_tools=("quote",)))
            self.assertEqual("fail", result.status)

    def test_default_basket_is_explicit_and_preserves_personal_shared_provenance(self):
        rendered = format_market_watchlist_block(SimpleNamespace(
            personal=("SNDK", "MU", "SPCX", "INTC", "AMD", "NVDA", "QQQ", "SPY"),
            shared=("GOOG", "SNDK", "MU", "AMD", "INTC", "MSFT"),
        ))
        self.assertIn("Personal: SNDK, MU, SPCX, INTC, AMD, NVDA, QQQ, SPY", rendered)
        self.assertIn("Shared market-report defaults: GOOG, SNDK, MU, AMD, INTC, MSFT", rendered)
        self.assertIn("Default quote basket: SNDK, MU, SPCX, INTC, AMD, NVDA, QQQ, SPY, GOOG, MSFT", rendered)

    def test_guidance_resolves_quote_sense_and_keeps_the_complete_basket(self):
        guidance = format_available_tool_guidance(
            available_tool_names={"quote"}, market_watchlist_symbols=("NVDA", "AMD", "MU"),
        )
        self.assertIn("quotation", guidance)
        self.assertIn("24-hour", guidance)
        self.assertIn("complete default basket", guidance)
        self.assertIn("Preserve the pending basket", guidance)
        self.assertIn("indices only", guidance)
        self.assertIn("listing a watchlist does not request live prices", guidance)
