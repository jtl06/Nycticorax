from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from nycti.chat.run_state import ToolStatus
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)

class _FakeMarketDataClient:
    def __init__(self) -> None:
        self.quote_error: Exception | None = None
        self.quote_result = None
        self.history_error: Exception | None = None
        self.history_result = None
        self.history_results: list[object] = []
        self.history_calls: list[dict[str, object]] = []
        self.search_result: list[object] = []
        self.search_calls: list[str] = []

    async def get_market_quote(self, symbol: str, **kwargs):  # type: ignore[no-untyped-def]
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote_result

    async def search_symbols(self, symbol: str):  # type: ignore[no-untyped-def]
        self.search_calls.append(symbol)
        return self.search_result

    async def get_price_history(  # type: ignore[no-untyped-def]
        self,
        symbol: str,
        *,
        interval: str,
        outputsize: int,
        start_date: str | None,
        end_date: str | None,
        **kwargs,
    ):
        self.history_calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        if self.history_error is not None:
            raise self.history_error
        if self.history_results:
            return self.history_results.pop(0)
        return self.history_result


class _FakeYahooFinanceClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.quote_result = None
        self.quote_error: Exception | None = None
        self.snapshot_calls: list[str] = []
        self.snapshot_result = None
        self.snapshot_error: Exception | None = None

    async def get_extended_hours_quote(self, symbol: str):  # type: ignore[no-untyped-def]
        self.calls.append(symbol)
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote_result

    async def get_market_snapshot(self, symbol: str):  # type: ignore[no-untyped-def]
        self.snapshot_calls.append(symbol)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot_result


class _FakeHistoryChannel:
    def __init__(self, messages: list[object], source_message: object) -> None:
        self.messages = messages
        self.source_message = source_message

    async def fetch_message(self, message_id: int):  # type: ignore[no-untyped-def]
        return self.source_message

    async def history(self, *, limit: int, before: object, oldest_first: bool):  # type: ignore[no-untyped-def]
        selected = list(reversed(self.messages))[:limit]
        if oldest_first:
            selected = list(reversed(selected))
        for item in selected:
            yield item


class _FakeBrowserClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []
        self.result = SimpleNamespace(
            requested_url="https://example.com/page",
            final_url="https://example.com/page",
            title="Example Title",
            content="Example extracted content",
        )

    async def extract(self, *, url: str, query: str | None, headed: bool):  # type: ignore[no-untyped-def]
        self.calls.append((url, query, headed))
        return self.result


class _FakeYouTubeTranscriptClient:
    def __init__(self) -> None:
        from nycti.youtube.models import YouTubeTranscriptResponse, YouTubeTranscriptSegment

        self.calls: list[str] = []
        self.result = YouTubeTranscriptResponse(
            video_id="dQw4w9WgXcQ",
            requested_url="https://youtu.be/dQw4w9WgXcQ",
            transcript_url="https://video.google.com/timedtext?v=dQw4w9WgXcQ",
            language_code="en",
            language_name="English",
            is_generated=False,
            segments=[
                YouTubeTranscriptSegment(start_seconds=0.0, duration_seconds=3.0, text="Opening line"),
                YouTubeTranscriptSegment(start_seconds=3.0, duration_seconds=3.0, text="Focused chorus line"),
            ],
        )

    async def get_transcript(self, *, url: str):  # type: ignore[no-untyped-def]
        self.calls.append(url)
        return self.result


class _FakeTranscriptSummaryLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="The video opens briefly, then discusses the focused chorus line.",
            usage=SimpleNamespace(total_tokens=37),
        )


class ChatToolExecutorStockQuoteTests(unittest.IsolatedAsyncioTestCase):
    def _build_executor(
        self,
        market_data_client: _FakeMarketDataClient,
        yahoo_finance_client: _FakeYahooFinanceClient | None = None,
    ) -> ChatToolExecutor:
        return ChatToolExecutor(
            database=SimpleNamespace(),
            settings=SimpleNamespace(channel_context_limit=12, openai_memory_model="cheap-model"),
            llm_client=SimpleNamespace(),
            market_data_client=market_data_client,
            tavily_client=SimpleNamespace(),
            yahoo_finance_client=yahoo_finance_client,
            browser_client=None,
            memory_service=SimpleNamespace(),
            channel_alias_service=SimpleNamespace(),
            reminder_service=SimpleNamespace(),
            bot=SimpleNamespace(),
        )

    async def test_single_stock_quote_surfaces_provider_error_without_symbol_lookup_retry(self) -> None:
        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_error = Exception("placeholder")
        from nycti.twelvedata.models import TwelveDataHTTPError

        market_data_client.quote_error = TwelveDataHTTPError("API key is invalid.")
        executor = self._build_executor(market_data_client)

        result = await executor._execute_single_stock_quote_tool(symbol="SPX")

        self.assertEqual(result, "Market quote for `SPX` failed: API key is invalid.")
        self.assertEqual(market_data_client.search_calls, [])

    async def test_stock_quote_uses_yahoo_when_primary_provider_credits_are_exhausted(self) -> None:
        from nycti.twelvedata.models import TwelveDataHTTPError
        from nycti.yahoo.models import YahooExtendedHoursQuote

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_error = TwelveDataHTTPError("API credits exhausted.")
        yahoo_finance_client = _FakeYahooFinanceClient()
        yahoo_finance_client.quote_result = YahooExtendedHoursQuote(
            symbol="NVDA",
            price=205.50,
            timestamp=1_776_806_400,
            session="post",
            currency="USD",
            exchange_name="NMS",
            timezone_name="America/New_York",
            market_state="POST",
            regular_price=201.00,
        )
        executor = self._build_executor(market_data_client, yahoo_finance_client)

        execution = await executor.execute(
            tool_name=STOCK_QUOTE_TOOL_NAME,
            arguments='{"symbols":["NVDA"]}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(ToolStatus.OK, execution.status)
        self.assertEqual(1, execution.metrics["stock_quote_success_symbol_count"])
        self.assertEqual("yahoo", execution.metrics["market_data_provider"])
        self.assertIn("Primary quote provider was unavailable", execution.content)

    async def test_single_stock_quote_uses_symbol_search_for_lookup_style_errors(self) -> None:
        from nycti.twelvedata.models import TwelveDataHTTPError, TwelveDataSymbolMatch

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_error = TwelveDataHTTPError("Symbol not found.")
        market_data_client.search_result = [
            TwelveDataSymbolMatch(
                symbol="ES",
                instrument_name="E-mini S&P 500",
                exchange="CME",
                instrument_type="Future",
                country="United States",
            )
        ]
        executor = self._build_executor(market_data_client)

        result = await executor._execute_single_stock_quote_tool(symbol="ES=F")

        self.assertIn("could not quote `ES=F` directly", result)
        self.assertEqual(market_data_client.search_calls, ["ES=F"])

    async def test_execute_stock_quote_keeps_tool_call_count_separate_from_symbol_count(self) -> None:
        from nycti.twelvedata.models import TwelveDataQuote

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_result = TwelveDataQuote(
            symbol="SPX",
            name="S&P 500 Index",
            exchange="CBOE",
            instrument_type="Index",
            currency="USD",
            datetime="2026-04-09 16:00:00",
            close=5234.12,
            previous_close=5200.00,
            change=34.12,
            percent_change=0.66,
        )
        executor = self._build_executor(market_data_client)

        execution = await executor.execute(
            tool_name=STOCK_QUOTE_TOOL_NAME,
            arguments='{"symbols":["SPX","ES"]}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(execution.metrics["stock_quote_count"], 1)
        self.assertEqual(execution.metrics["stock_quote_symbol_count"], 2)
        self.assertEqual(execution.metrics["stock_quote_success_symbol_count"], 2)

    async def test_single_stock_quote_adds_yahoo_extended_hours_when_market_closed(self) -> None:
        from nycti.twelvedata.models import TwelveDataQuote
        from nycti.yahoo.models import YahooExtendedHoursQuote

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_result = TwelveDataQuote(
            symbol="NVDA",
            name="NVIDIA Corp",
            exchange="NASDAQ",
            instrument_type="Common Stock",
            currency="USD",
            datetime="2026-04-28 16:00:00",
            close=200.00,
            previous_close=198.00,
            change=2.00,
            percent_change=1.01,
            is_market_open=False,
        )
        yahoo_finance_client = _FakeYahooFinanceClient()
        yahoo_finance_client.quote_result = YahooExtendedHoursQuote(
            symbol="NVDA",
            price=205.50,
            timestamp=1_776_806_400,
            session="post",
            currency="USD",
            exchange_name="NMS",
            timezone_name="America/New_York",
            market_state="POST",
            regular_price=201.00,
        )
        executor = self._build_executor(market_data_client, yahoo_finance_client)

        result = await executor._execute_single_stock_quote_tool(symbol="NVDA")

        self.assertNotIn("Last price: USD 200.0000", result)
        self.assertIn(
            "Current provider identity: NVDA resolves to NVIDIA Corp on NASDAQ",
            result,
        )
        self.assertIn(
            "Yahoo's same-page regular and extended-hours prices override",
            result,
        )
        self.assertIn("Yahoo Finance extended-hours fallback for: NVDA | NMS", result)
        self.assertIn("After-hours price: USD 205.5000", result)
        self.assertIn("Regular close (Yahoo): USD 201.0000", result)
        self.assertIn(
            "Extended-hours change: +4.5000 (+2.24%) vs Yahoo regular close 201.0000",
            result,
        )
        self.assertIn(
            "Provider conflict: Twelve Data close 200.0000 differs from Yahoo regular close 201.0000",
            result,
        )
        self.assertEqual(1, executor._stock_quote_success_count(result))
        self.assertEqual("twelvedata+yahoo", executor._stock_quote_provider(result))
        self.assertEqual(yahoo_finance_client.calls, ["NVDA"])

    async def test_single_stock_quote_skips_yahoo_when_market_open(self) -> None:
        from nycti.twelvedata.models import TwelveDataQuote
        from nycti.yahoo.models import YahooMarketSnapshot

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_result = TwelveDataQuote(
            symbol="NVDA",
            name="NVIDIA Corp",
            exchange="NASDAQ",
            instrument_type="Common Stock",
            currency="USD",
            datetime="2026-04-28 12:00:00",
            close=200.00,
            previous_close=198.00,
            change=2.00,
            percent_change=1.01,
            is_market_open=True,
        )
        yahoo_finance_client = _FakeYahooFinanceClient()
        yahoo_finance_client.snapshot_result = YahooMarketSnapshot(
            symbol="NVDA",
            currency="USD",
            exchange_name="NasdaqGS",
            timezone_name="America/New_York",
            market_state="REGULAR",
            regular_price=200.0,
            regular_timestamp=1_777_410_000,
            market_cap=4_800_000_000_000,
            shares_outstanding=24_000_000_000,
        )
        executor = self._build_executor(market_data_client, yahoo_finance_client)

        result = await executor._execute_single_stock_quote_tool(symbol="NVDA")

        self.assertIn("Yahoo Finance public-company valuation for: NVDA | NasdaqGS", result)
        self.assertIn("Market cap (regular-price basis): USD 4.8000T", result)
        self.assertIn("Shares outstanding: 24.0000B", result)
        self.assertNotIn("extended-hours fallback", result)
        self.assertEqual(yahoo_finance_client.snapshot_calls, ["NVDA"])
        self.assertEqual(yahoo_finance_client.calls, [])

    async def test_single_stock_quote_tries_yahoo_when_market_open_is_unknown(self) -> None:
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        from datetime import datetime

        from nycti.twelvedata.models import TwelveDataQuote
        from nycti.yahoo.models import YahooExtendedHoursQuote

        market_data_client = _FakeMarketDataClient()
        market_data_client.quote_result = TwelveDataQuote(
            symbol="STX",
            name="Seagate Technology Holdings plc",
            exchange="NASDAQ",
            instrument_type="Common Stock",
            currency="USD",
            datetime="2026-04-28 16:00:00",
            close=595.86,
            previous_close=595.86,
            change=None,
            percent_change=None,
            is_market_open=None,
        )
        yahoo_finance_client = _FakeYahooFinanceClient()
        yahoo_finance_client.quote_result = YahooExtendedHoursQuote(
            symbol="STX",
            price=579.31,
            timestamp=1_777_410_092,
            session="post",
            currency="USD",
            exchange_name="NMS",
            timezone_name="America/New_York",
            market_state="POST",
        )
        executor = self._build_executor(market_data_client, yahoo_finance_client)

        with patch(
            "nycti.chat.tools.market.datetime",
            wraps=datetime,
        ) as datetime_mock:
            datetime_mock.now.return_value = datetime(2026, 4, 28, 17, 15, tzinfo=ZoneInfo("America/New_York"))
            result = await executor._execute_single_stock_quote_tool(symbol="STX")

        self.assertIn("Yahoo Finance extended-hours fallback for: STX | NMS", result)
        self.assertIn("After-hours price: USD 579.3100", result)
        self.assertEqual(yahoo_finance_client.calls, ["STX"])

    async def test_execute_price_history_exposes_metrics(self) -> None:
        from nycti.twelvedata.models import TwelveDataTimeSeries, TwelveDataTimeSeriesPoint

        market_data_client = _FakeMarketDataClient()
        market_data_client.history_result = TwelveDataTimeSeries(
            symbol="SPY",
            name="SPDR S&P 500 ETF Trust",
            exchange="NYSE",
            instrument_type="ETF",
            currency="USD",
            interval="1day",
            values=[TwelveDataTimeSeriesPoint(datetime="2026-04-09", close=679.86)],
        )
        executor = self._build_executor(market_data_client)

        execution = await executor.execute(
            tool_name=PRICE_HISTORY_TOOL_NAME,
            arguments='{"symbol":"SPY","interval":"1day","outputsize":5}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(execution.metrics["price_history_count"], 1)
        self.assertEqual(execution.metrics["price_history_symbol"], "SPY")
        self.assertEqual(execution.metrics["price_history_mode"], "recent")
        self.assertEqual(execution.metrics["price_history_interval"], "1day")
        self.assertEqual(execution.metrics["price_history_status"], "ok")

    async def test_execute_price_history_extrema_processes_without_raw_candles(self) -> None:
        from nycti.twelvedata.models import TwelveDataTimeSeries, TwelveDataTimeSeriesPoint

        market_data_client = _FakeMarketDataClient()
        market_data_client.history_result = TwelveDataTimeSeries(
            symbol="INTC",
            name="Intel Corporation",
            exchange="NASDAQ",
            instrument_type="Common Stock",
            currency="USD",
            interval="1day",
            values=[
                TwelveDataTimeSeriesPoint(
                    datetime="2026-07-15", high=109.49, low=99.20, close=100.72
                ),
                TwelveDataTimeSeriesPoint(
                    datetime="2026-06-30", high=142.35, low=136.00, close=140.94
                ),
            ],
        )
        executor = self._build_executor(market_data_client)

        execution = await executor.execute(
            tool_name=PRICE_HISTORY_TOOL_NAME,
            arguments='{"symbol":"INTC","mode":"extrema","interval":null,"outputsize":null,"start_date":null,"end_date":null}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(execution.status, ToolStatus.OK)
        self.assertEqual(execution.metrics["price_history_mode"], "extrema")
        self.assertIn("Highest intraday: USD 142.3500 on 2026-06-30", execution.content)
        self.assertIn("raw candles were not sent to the model", execution.content)
        self.assertNotIn("Recent candles:", execution.content)
        self.assertEqual(market_data_client.history_calls[0]["outputsize"], 5000)

    async def test_execute_get_channel_context_raw_fetches_older_window(self) -> None:
        messages = [
            SimpleNamespace(
                id=index,
                content=f"message {index}",
                attachments=[],
                author=SimpleNamespace(display_name=f"user{index}"),
                created_at=datetime(2026, 4, 12, 20, index, tzinfo=timezone.utc),
            )
            for index in range(8)
        ]
        source_message = SimpleNamespace(id=99)
        channel = _FakeHistoryChannel(messages, source_message)
        executor = ChatToolExecutor(
            database=SimpleNamespace(),
            settings=SimpleNamespace(channel_context_limit=2, openai_memory_model="cheap-model"),
            llm_client=SimpleNamespace(),
            market_data_client=_FakeMarketDataClient(),
            tavily_client=SimpleNamespace(),
            memory_service=SimpleNamespace(),
            channel_alias_service=SimpleNamespace(),
            reminder_service=SimpleNamespace(),
            bot=SimpleNamespace(get_channel=lambda channel_id: channel),
        )

        execution = await executor.execute(
            tool_name=GET_CHANNEL_CONTEXT_TOOL_NAME,
            arguments='{"mode":"raw","multiplier":1}',
            guild_id=1,
            channel_id=123,
            user_id=1,
            source_message_id=99,
        )

        self.assertIn("Older Discord channel context (raw", execution.content)
        self.assertIn("Do not paste this block verbatim", execution.content)
        self.assertIn("Per-line text cap: 280 chars", execution.content)
        self.assertIn("user1: message 1", execution.content)
        self.assertIn("user5: message 5", execution.content)
        self.assertNotIn("user6: message 6", execution.content)
        self.assertEqual(execution.metrics["channel_context_mode"], "raw")
        self.assertEqual(execution.metrics["channel_context_status"], "ok")
        self.assertEqual(execution.metrics["channel_context_expand"], "no")

    async def test_execute_browser_extract_tool_uses_browser_client(self) -> None:
        browser_client = _FakeBrowserClient()
        executor = ChatToolExecutor(
            database=SimpleNamespace(),
            settings=SimpleNamespace(channel_context_limit=2, openai_memory_model="cheap-model"),
            llm_client=SimpleNamespace(),
            market_data_client=_FakeMarketDataClient(),
            tavily_client=SimpleNamespace(),
            browser_client=browser_client,
            memory_service=SimpleNamespace(),
            channel_alias_service=SimpleNamespace(),
            reminder_service=SimpleNamespace(),
            bot=SimpleNamespace(get_channel=lambda _: None),
        )

        execution = await executor.execute(
            tool_name=BROWSER_EXTRACT_TOOL_NAME,
            arguments='{"url":"https://example.com/page","query":"example","headed":true}',
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=4,
        )

        self.assertIn("Browser extract for: https://example.com/page", execution.content)
        self.assertIn("Title: Example Title", execution.content)
        self.assertEqual(browser_client.calls, [("https://example.com/page", "example", True)])
        self.assertEqual(execution.metrics["browser_extract_count"], 1)
        self.assertEqual(execution.metrics["browser_extract_headed"], "yes")

    async def test_execute_youtube_transcript_tool_uses_youtube_client(self) -> None:
        youtube_client = _FakeYouTubeTranscriptClient()
        llm_client = _FakeTranscriptSummaryLLM()
        executor = ChatToolExecutor(
            database=SimpleNamespace(),
            settings=SimpleNamespace(
                channel_context_limit=2,
                openai_memory_model="cheap-model",
                youtube_transcript_max_chars=2000,
            ),
            llm_client=llm_client,
            market_data_client=_FakeMarketDataClient(),
            tavily_client=SimpleNamespace(),
            browser_client=None,
            youtube_client=youtube_client,
            memory_service=SimpleNamespace(),
            channel_alias_service=SimpleNamespace(),
            reminder_service=SimpleNamespace(),
            bot=SimpleNamespace(get_channel=lambda _: None),
        )

        execution = await executor.execute(
            tool_name=YOUTUBE_TRANSCRIPT_TOOL_NAME,
            arguments='{"url":"https://youtu.be/dQw4w9WgXcQ","query":"chorus"}',
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=4,
        )

        self.assertIn(
            "YouTube transcript summary for: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            execution.content,
        )
        self.assertIn("Focus: chorus", execution.content)
        self.assertIn("focused chorus line", execution.content)
        self.assertEqual(youtube_client.calls, ["https://youtu.be/dQw4w9WgXcQ"])
        self.assertEqual(llm_client.calls[0]["model"], "cheap-model")
        self.assertEqual(llm_client.calls[0]["feature"], "youtube_transcript_summary")
        self.assertIn("Focused chorus line", llm_client.calls[0]["messages"][1]["content"])
        self.assertEqual(execution.metrics["youtube_transcript_count"], 1)
        self.assertEqual(execution.metrics["youtube_transcript_status"], "ok")
        self.assertEqual(execution.metrics["youtube_transcript_summary_tokens"], 37)


if __name__ == "__main__":
    unittest.main()
