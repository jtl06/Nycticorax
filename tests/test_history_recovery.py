import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from nycti.chat.run_state import AgentPermissions, AgentRun, ToolOutcome, ToolStatus
from nycti.chat.loop_messages import fallback_text
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.parsing import parse_price_history_arguments
from nycti.chat.orchestrator_support import looks_like_raw_tool_dump
from nycti.chat.finalization import finalize_run
from nycti.agent_trace import AgentTrace
from nycti.twelvedata.client import TwelveDataClient
from nycti.twelvedata.models import TwelveDataHTTPError, TwelveDataRateLimitError
from nycti.yahoo.client import YahooFinanceClient, YahooFinanceDataError
from nycti.yahoo.models import YahooMarketSnapshot
from nycti.chat.tools.market_page_data import MarketPageObservation


NOW = datetime(2026, 11, 4, 18, tzinfo=timezone.utc)


def chart(symbol):
    dates = ("2026-10-23T20:00:00+00:00", "2026-10-29T20:00:00+00:00", "2026-10-30T20:00:00+00:00")
    return {"chart": {"error": None, "result": [{"meta": {"symbol": symbol, "currency": "USD",
        "exchangeTimezoneName": "America/New_York"},
        "timestamp": [int(datetime.fromisoformat(day).timestamp()) for day in dates],
        "indicators": {"quote": [{"open": [100, 110, 90], "high": [105, 120, 95],
                                   "low": [98, 108, 88], "close": [101, 115, 92]}]}}]}}


def executor(primary, alternate):
    result = ChatToolExecutor(database=SimpleNamespace(), settings=SimpleNamespace(),
        llm_client=SimpleNamespace(), market_data_client=primary, yahoo_finance_client=alternate,
        tavily_client=SimpleNamespace(), memory_service=SimpleNamespace(),
        channel_alias_service=SimpleNamespace(), reminder_service=SimpleNamespace(), bot=SimpleNamespace())
    # All tests are network isolated, including the last-resort public-page path.
    result.market_page_client = SimpleNamespace(get=AsyncMock(side_effect=ValueError("offline fixture")))
    return result


async def execute(tool, arguments):
    return await tool.execute(tool_name="price_hist", arguments=json.dumps(arguments),
        guild_id=1, channel_id=2, user_id=3, source_message_id=4,
        permissions=AgentPermissions(), run_id="history-regression", step_index=1)


class HistoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_bounds_use_exchange_dates_not_utc_dates(self):
        payload = chart("AAA")
        result = payload["chart"]["result"][0]
        result["meta"]["exchangeTimezoneName"] = "Asia/Tokyo"
        result["timestamp"] = [int(datetime(2026, 10, day, 15, tzinfo=timezone.utc).timestamp()) for day in (22, 23)]
        for key, values in result["indicators"]["quote"][0].items():
            result["indicators"]["quote"][0][key] = values[:2]
        yahoo = YahooFinanceClient(fetch_json=lambda _: payload, now=lambda: NOW)
        series = await yahoo.get_price_history("AAA", start_date="2026-10-23", end_date="2026-10-23")
        self.assertEqual(["2026-10-23"], [point.datetime for point in series.values])
        self.assertEqual(101, series.values[0].close)

    async def test_reported_peak_fallback_does_not_fabricate_historical_closes(self):
        yahoo = SimpleNamespace(get_price_history=AsyncMock(side_effect=YahooFinanceDataError("offline")))
        tool = executor(TwelveDataClient(None), yahoo)
        tool.market_page_client.get = AsyncMock(return_value=MarketPageObservation(
            url="https://stockscan.io/stocks/AAA/price-history", symbol="AAA", observed_at=NOW.isoformat(),
            reported_ath=150, reported_ath_date="2026-10-06", adjustment_basis="split/dividend adjusted"))
        result = await execute(tool, {"symbol": "AAA", "mode": "extrema"})
        self.assertEqual(ToolStatus.OK, result.status)
        self.assertIn("not a complete candle series", result.content)
        self.assertEqual("stockscan", result.metrics["price_history_provider"])
        tool.market_page_client.get.reset_mock()
        bounded = await execute(tool, {"symbol": "AAA", "mode": "extrema", "start_date": "2026-10-23"})
        self.assertEqual(ToolStatus.ERROR, bounded.status)
        tool.market_page_client.get.assert_not_awaited()

    async def test_slow_symbol_does_not_discard_completed_batch_results(self):
        tool = executor(TwelveDataClient(None), None)
        cancelled = []

        async def one(*, symbol, **kwargs):
            if symbol == "SLOW":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(symbol)
            return f"Yahoo Finance price history for: {symbol} ({symbol})\n2026-10-23: close 100"

        tool._execute_price_history_tool = one
        with patch("nycti.chat.tools.handlers.HISTORY_BATCH_TIMEOUT_SECONDS", 0.01):
            result = await execute(tool, {"symbols": ["AAA", "SLOW", "BBB"]})
        self.assertEqual("partial", result.metrics["price_history_status"])
        self.assertEqual(2, result.metrics["price_history_success_symbol_count"])
        self.assertIn("`SLOW` unavailable: batch deadline", result.content)
        self.assertIn("(AAA)", result.content)
        self.assertIn("(BBB)", result.content)
        self.assertEqual(["SLOW"], cancelled)

    async def test_market_cap_survives_primary_quote_failure(self):
        snapshot = YahooMarketSnapshot(symbol="AAA", currency="USD", market_state="REGULAR",
            regular_price=100, regular_timestamp=int(NOW.timestamp()), market_cap=1e12,
            shares_outstanding=1e10, timezone_name="America/New_York")
        yahoo = SimpleNamespace(get_market_snapshot=AsyncMock(return_value=snapshot))
        tool = executor(TwelveDataClient(None), yahoo)
        text = await tool._execute_single_stock_quote_tool(symbol="AAA")
        self.assertIn("current-session fallback", text)
        self.assertIn("Market cap (regular-price basis): USD 1.0000T", text)
        self.assertIn("Shares outstanding:", text)
        yahoo.get_market_snapshot.assert_awaited_once()

    async def test_bad_symbol_does_not_activate_provider_cooldown(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise TwelveDataHTTPError("symbol not found")

        client = TwelveDataClient("fixture", fetch_json=fetch)
        for symbol in ("BAD", "AAA"):
            with self.assertRaises(TwelveDataHTTPError) as caught:
                await client.get_price_history(symbol)
            self.assertNotIsInstance(caught.exception, TwelveDataRateLimitError)
        self.assertEqual(2, len(calls))

    async def test_final_failure_or_raw_market_output_never_leaks_quote_dump(self):
        raw = "Twelve Data market quote for: AAA\nLast price: USD 100\nInstrument: NASDAQ"
        for model in (AsyncMock(side_effect=TimeoutError()), AsyncMock(return_value=SimpleNamespace(
                text=raw, reasoning_content="", raw_text=raw, response_output_items=[]))):
            run = AgentRun(messages=[], request_text="find the top 10 market cap stonk now")
            run.outcomes.append(ToolOutcome(call_id="q", tool_name="quote", arguments='{}',
                status=ToolStatus.OK, content=raw))
            answer, _ = await finalize_run(run=run, call_model=model, chat_model="fixture",
                final_max_tokens=500, continuation_max_tokens=200, metrics={}, trace=AgentTrace(enabled=False))
            self.assertLess(len(answer), 150)
            self.assertNotIn("Instrument:", answer)
            self.assertNotIn("market quote for:", answer)
            self.assertEqual("fallback", run.final_status)

    async def test_shared_cooldown_blocks_quote_and_history_until_retry_window(self):
        clock = [0.0]
        calls = []

        def fetch(url):
            calls.append(url)
            return {"status": "error", "code": 429, "message": "credit quota exhausted"}

        client = TwelveDataClient("fixture", fetch_json=fetch, clock=lambda: clock[0])
        results = await asyncio.gather(client.get_market_quote("AAA"),
            *(client.get_price_history(f"T{i}") for i in range(18)), return_exceptions=True)
        self.assertTrue(all(isinstance(item, TwelveDataRateLimitError) for item in results))
        self.assertEqual(1, len(calls))
        clock[0] = 59.9
        with self.assertRaises(TwelveDataRateLimitError):
            await client.get_market_quote("BBB")
        self.assertEqual(1, len(calls))
        clock[0] = 60
        with self.assertRaises(TwelveDataRateLimitError):
            await client.get_price_history("BBB")
        self.assertEqual(2, len(calls))

    async def test_18_stock_peak_batch_recovers_without_exhausting_tool_calls(self):
        calls = []
        active = [0, 0]

        def limited(url):
            calls.append(url)
            raise TwelveDataHTTPError("You have run out of API credits for the current minute")

        yahoo = YahooFinanceClient(fetch_json=lambda url: chart(urlsplit(url).path.split("/")[-1]), now=lambda: NOW)
        original = yahoo.get_price_history

        async def tracked(*args, **kwargs):
            active[0] += 1
            active[1] = max(active)
            await asyncio.sleep(0)
            try:
                return await original(*args, **kwargs)
            finally:
                active[0] -= 1

        yahoo.get_price_history = tracked
        tool = executor(TwelveDataClient("fixture", fetch_json=limited), yahoo)
        symbols = [f"T{i}" for i in range(18)]
        result = await execute(tool, {"symbols": symbols, "mode": "extrema"})
        self.assertEqual(ToolStatus.OK, result.status)
        self.assertEqual(18, result.metrics["price_history_success_symbol_count"])
        self.assertEqual(1, result.metrics["price_history_count"])
        self.assertEqual(1, len(calls))
        self.assertLessEqual(active[1], 3)
        for symbol in symbols:
            self.assertIn(f"({symbol})", result.content)
        self.assertIn("Highest intraday: USD 120.0000", result.content)
        self.assertIn("do not call them all-time", result.content)
        self.assertNotIn("Full requested/provider range scanned: yes", result.content)
        self.assertEqual(0, len(yahoo._history_cache._values))

    async def test_weekly_range_keeps_friday_close_and_partial_symbol_failure(self):
        urls = []

        def fetch(url):
            urls.append(url)
            symbol = urlsplit(url).path.split("/")[-1]
            return chart(symbol) if symbol != "BAD" else {"chart": {"error": {"description": "not found"}}}

        yahoo = YahooFinanceClient(fetch_json=fetch, now=lambda: NOW)
        tool = executor(TwelveDataClient(None), yahoo)
        result = await execute(tool, {"symbols": ["AAA", "BAD", "BBB"], "start_date": "2026-10-23",
                                     "end_date": "2026-10-29", "outputsize": 8})
        self.assertEqual(ToolStatus.OK, result.status)
        self.assertEqual("partial", result.metrics["price_history_status"])
        self.assertEqual(2, result.metrics["price_history_success_symbol_count"])
        self.assertEqual(1, result.metrics["price_history_failed_symbol_count"])
        self.assertIn("2026-10-23: close 101.0000", result.content)
        self.assertIn("2026-10-29: close 115.0000", result.content)
        self.assertNotIn("2026-10-30: close", result.content)
        self.assertIn("`BAD` unavailable", result.content)
        self.assertIn("Yahoo Finance", result.content)
        query = parse_qs(urlsplit(urls[0]).query)
        self.assertEqual("1d", query["interval"][0])
        self.assertEqual("2026-10-31", datetime.fromtimestamp(int(query["period2"][0]), timezone.utc).date().isoformat())
        tool.market_page_client.get.assert_not_awaited()

    async def test_yahoo_identity_and_invalid_values_are_rejected(self):
        for invalid in (chart("WRONG"), chart("AAA")):
            if invalid["chart"]["result"][0]["meta"]["symbol"] == "AAA":
                invalid["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = float("nan")
            yahoo = YahooFinanceClient(fetch_json=lambda _: invalid, now=lambda: NOW)
            with self.assertRaises(YahooFinanceDataError):
                await yahoo.get_price_history("AAA", start_date="2026-10-23", end_date="2026-10-30")

    async def test_all_provider_failures_remain_errors_not_successful_history(self):
        yahoo = SimpleNamespace(get_price_history=AsyncMock(side_effect=YahooFinanceDataError("offline")))
        tool = executor(TwelveDataClient(None), yahoo)
        result = await execute(tool, {"symbols": ["AAA", "BBB"], "mode": "extrema"})
        self.assertEqual(ToolStatus.ERROR, result.status)
        self.assertEqual(0, result.metrics["price_history_success_symbol_count"])
        self.assertIn("`AAA` unavailable", result.content)
        self.assertIn("`BBB` unavailable", result.content)


class HistoryContractTests(unittest.TestCase):
    def test_batch_is_bounded_and_single_symbol_is_compatible(self):
        self.assertEqual(("AAA",), parse_price_history_arguments('{"symbol":"aaa"}').symbols)
        self.assertEqual(("AAA", "BBB"), parse_price_history_arguments('{"symbols":["aaa","BBB","AAA"]}').symbols)
        self.assertIsNone(parse_price_history_arguments(json.dumps({"symbols": [f"T{i}" for i in range(21)]})))
        for payload in ({"symbols": [True]}, {"symbols": "AAA"}, {"symbol": "AAA", "symbols": ["BBB"]}):
            self.assertIsNone(parse_price_history_arguments(json.dumps(payload)))

    def test_top_ten_request_never_receives_raw_market_fallback(self):
        raw = ("Twelve Data market quote for: Apple Inc. (AAPL)\nInstrument: NASDAQ\nLast price: USD 352.64\n\n"
               "Yahoo Finance public-company valuation for: AAPL | NasdaqGS\nMarket cap (regular-price basis): USD 5.1238T")
        run = AgentRun(messages=[], request_text="find the top 10 market cap stonk now")
        run.outcomes.append(ToolOutcome(call_id="q", tool_name="quote", arguments='{}',
                                       status=ToolStatus.OK, content=raw))
        answer = fallback_text(run)
        self.assertLess(len(answer), 150)
        self.assertNotIn("Instrument:", answer)
        self.assertNotIn("market quote for:", answer)
        self.assertIn("couldn't finish", answer)
        self.assertTrue(looks_like_raw_tool_dump("Here is the data: " + raw))
        self.assertFalse(looks_like_raw_tool_dump("1. Apple (AAPL): $5.12T\n2. Microsoft (MSFT): $4.13T"))
