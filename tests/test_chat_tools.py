from types import SimpleNamespace
import unittest

from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.parsing import (
    parse_create_reminder_arguments,
    parse_extract_url_arguments,
    parse_price_history_arguments,
    parse_send_channel_message_arguments,
    parse_tool_query_argument,
    parse_tool_symbol_list_arguments,
)
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    build_chat_tools,
)


class ChatToolParsingTests(unittest.TestCase):
    def test_parse_tool_query_argument_returns_query_string(self) -> None:
        self.assertEqual(
            parse_tool_query_argument('{"query":"latest nvda earnings"}'),
            "latest nvda earnings",
        )

    def test_parse_create_reminder_arguments_requires_both_fields(self) -> None:
        payload = parse_create_reminder_arguments('{"message":"check NVDA","remind_at":"2026-03-22"}')
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.message, "check NVDA")
        self.assertEqual(payload.remind_at, "2026-03-22")
        self.assertIsNone(parse_create_reminder_arguments('{"message":"check NVDA"}'))

    def test_parse_send_channel_message_arguments_requires_both_fields(self) -> None:
        payload = parse_send_channel_message_arguments('{"channel":"alerts","message":"deploy live"}')
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.channel, "alerts")
        self.assertEqual(payload.message, "deploy live")
        self.assertIsNone(parse_send_channel_message_arguments('{"channel":"alerts"}'))

    def test_parse_extract_url_arguments_requires_url(self) -> None:
        payload = parse_extract_url_arguments('{"url":"https://example.com/post","query":"latest guidance"}')
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.url, "https://example.com/post")
        self.assertEqual(payload.query, "latest guidance")
        self.assertIsNone(parse_extract_url_arguments('{"query":"latest guidance"}'))

    def test_parse_tool_symbol_list_arguments_accepts_comma_separated_symbol_string(self) -> None:
        self.assertEqual(
            parse_tool_symbol_list_arguments('{"symbol":"SPX, ES, NQ"}'),
            ["SPX", "ES", "NQ"],
        )

    def test_parse_tool_symbol_list_arguments_accepts_symbols_array_and_dedupes(self) -> None:
        self.assertEqual(
            parse_tool_symbol_list_arguments('{"symbols":["SPX", "es", "SPX", "NQ", "RTY", "YM"]}'),
            ["SPX", "ES", "NQ", "RTY", "YM"],
        )

    def test_parse_price_history_arguments_parses_optional_fields(self) -> None:
        payload = parse_price_history_arguments(
            '{"symbol":"spy","interval":"1week","outputsize":8,"start_date":"2026-01-01","end_date":"2026-03-31"}'
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.symbol, "SPY")
        self.assertEqual(payload.interval, "1week")
        self.assertEqual(payload.outputsize, 8)
        self.assertEqual(payload.start_date, "2026-01-01")
        self.assertEqual(payload.end_date, "2026-03-31")

    def test_parse_price_history_arguments_rejects_bad_outputsize(self) -> None:
        self.assertIsNone(parse_price_history_arguments('{"symbol":"SPY","outputsize":99}'))


class ChatToolSchemaTests(unittest.TestCase):
    def test_build_chat_tools_returns_expected_tool_names(self) -> None:
        names = [
            tool["function"]["name"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        ]
        self.assertEqual(
            names,
            [
                WEB_SEARCH_TOOL_NAME,
                STOCK_QUOTE_TOOL_NAME,
                PRICE_HISTORY_TOOL_NAME,
                IMAGE_SEARCH_TOOL_NAME,
                EXTRACT_URL_TOOL_NAME,
                CREATE_REMINDER_TOOL_NAME,
                SEND_CHANNEL_MESSAGE_TOOL_NAME,
            ],
        )


class _FakeMarketDataClient:
    def __init__(self) -> None:
        self.quote_error: Exception | None = None
        self.quote_result = None
        self.history_error: Exception | None = None
        self.history_result = None
        self.search_result: list[object] = []
        self.search_calls: list[str] = []

    async def get_market_quote(self, symbol: str):  # type: ignore[no-untyped-def]
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
    ):
        if self.history_error is not None:
            raise self.history_error
        return self.history_result


class ChatToolExecutorStockQuoteTests(unittest.IsolatedAsyncioTestCase):
    def _build_executor(self, market_data_client: _FakeMarketDataClient) -> ChatToolExecutor:
        return ChatToolExecutor(
            database=SimpleNamespace(),
            market_data_client=market_data_client,
            tavily_client=SimpleNamespace(),
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

        _, metrics = await executor.execute(
            tool_name=STOCK_QUOTE_TOOL_NAME,
            arguments='{"symbols":["SPX","ES"]}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(metrics["stock_quote_count"], 1)
        self.assertEqual(metrics["stock_quote_symbol_count"], 2)

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

        _, metrics = await executor.execute(
            tool_name=PRICE_HISTORY_TOOL_NAME,
            arguments='{"symbol":"SPY","interval":"1day","outputsize":5}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
        )

        self.assertEqual(metrics["price_history_count"], 1)
        self.assertEqual(metrics["price_history_symbol"], "SPY")
        self.assertEqual(metrics["price_history_interval"], "1day")
        self.assertEqual(metrics["price_history_status"], "ok")


if __name__ == "__main__":
    unittest.main()
