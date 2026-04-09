from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from nycti.twelvedata.client import (
    TWELVE_DATA_BASE_URL,
    TWELVE_DATA_USER_AGENT,
    TwelveDataClient,
)
from nycti.twelvedata.formatting import (
    format_market_quote_message,
    format_price_history_message,
    format_symbol_suggestions_message,
)
from nycti.twelvedata.models import (
    TwelveDataAPIKeyMissingError,
    TwelveDataHTTPError,
    TwelveDataQuote,
    TwelveDataSymbolMatch,
    TwelveDataTimeSeries,
    TwelveDataTimeSeriesPoint,
)


class TwelveDataFormattingTests(unittest.TestCase):
    def test_format_market_quote_message_includes_core_fields(self) -> None:
        quote = TwelveDataQuote(
            symbol="SPX",
            name="S&P 500 Index",
            exchange="CBOE",
            instrument_type="Index",
            currency="USD",
            datetime="2026-04-08 16:00:00",
            close=5234.12,
            previous_close=5200.00,
            change=34.12,
            percent_change=0.66,
            high=5240.00,
            low=5188.50,
            open=5195.00,
            volume=None,
            is_market_open=False,
        )
        message = format_market_quote_message(quote)
        self.assertIn("Twelve Data market quote for: S&P 500 Index (SPX)", message)
        self.assertIn("Instrument: Index | CBOE", message)
        self.assertIn("Last price: USD 5234.1200", message)
        self.assertIn("Change: +34.1200 (+0.66%) vs prev close 5200.0000", message)
        self.assertIn("Market open: no", message)

    def test_format_symbol_suggestions_message_lists_matches(self) -> None:
        matches = [
            TwelveDataSymbolMatch(
                symbol="ES",
                instrument_name="E-mini S&P 500",
                exchange="CME",
                instrument_type="Future",
                country="United States",
            )
        ]
        message = format_symbol_suggestions_message("ES=F", matches)
        self.assertIn("could not quote `ES=F` directly", message)
        self.assertIn("`ES`: E-mini S&P 500 | Future | CME | United States", message)

    def test_format_price_history_message_includes_range_and_candles(self) -> None:
        series = TwelveDataTimeSeries(
            symbol="SPY",
            name="SPDR S&P 500 ETF Trust",
            exchange="NYSE",
            instrument_type="ETF",
            currency="USD",
            interval="1day",
            values=[
                TwelveDataTimeSeriesPoint(
                    datetime="2026-04-09",
                    open=675.10,
                    high=680.40,
                    low=674.55,
                    close=679.86,
                    volume=98_765_432,
                ),
                TwelveDataTimeSeriesPoint(
                    datetime="2026-04-08",
                    open=672.00,
                    high=676.00,
                    low=670.20,
                    close=675.35,
                    volume=87_654_321,
                ),
            ],
        )
        message = format_price_history_message(series)
        self.assertIn("Twelve Data price history for: SPDR S&P 500 ETF Trust (SPY)", message)
        self.assertIn("Series: 1day | ETF | NYSE", message)
        self.assertIn("Returned candles: 2", message)
        self.assertIn("Time range: 2026-04-08 -> 2026-04-09", message)
        self.assertIn("- 2026-04-09: close 679.8600 | open 675.1000 | high 680.4000 | low 674.5500 | volume 98,765,432", message)


class TwelveDataClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_market_quote_returns_structured_quote(self) -> None:
        captured_urls: list[str] = []

        def fake_fetch(url: str) -> object:
            captured_urls.append(url)
            return {
                "symbol": "SPX",
                "name": "S&P 500 Index",
                "exchange": "CBOE",
                "type": "Index",
                "currency": "USD",
                "datetime": "2026-04-08 16:00:00",
                "close": "5234.12",
                "previous_close": "5200.00",
                "change": "34.12",
                "percent_change": "0.66",
                "open": "5195.00",
                "high": "5240.00",
                "low": "5188.50",
                "is_market_open": "false",
            }

        client = TwelveDataClient("twelve-key", fetch_json=fake_fetch)
        quote = await client.get_market_quote("spx")
        self.assertEqual(quote.symbol, "SPX")
        self.assertEqual(quote.close, 5234.12)
        self.assertIn(f"{TWELVE_DATA_BASE_URL}/quote?symbol=SPX&apikey=twelve-key", captured_urls[0])

    async def test_search_symbols_returns_matches(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "data": [
                    {
                        "symbol": "ES",
                        "instrument_name": "E-mini S&P 500",
                        "exchange": "CME",
                        "instrument_type": "Future",
                        "country": "United States",
                    }
                ]
            }

        client = TwelveDataClient("twelve-key", fetch_json=fake_fetch)
        matches = await client.search_symbols("ES=F")
        self.assertEqual(matches[0].symbol, "ES")
        self.assertEqual(matches[0].instrument_type, "Future")

    async def test_get_price_history_returns_structured_series(self) -> None:
        captured_urls: list[str] = []

        def fake_fetch(url: str) -> object:
            captured_urls.append(url)
            return {
                "meta": {
                    "symbol": "SPY",
                    "interval": "1day",
                    "currency": "USD",
                    "exchange": "NYSE",
                    "type": "ETF",
                },
                "values": [
                    {
                        "datetime": "2026-04-09",
                        "open": "675.10",
                        "high": "680.40",
                        "low": "674.55",
                        "close": "679.86",
                        "volume": "98765432",
                    },
                    {
                        "datetime": "2026-04-08",
                        "open": "672.00",
                        "high": "676.00",
                        "low": "670.20",
                        "close": "675.35",
                        "volume": "87654321",
                    },
                ],
            }

        client = TwelveDataClient("twelve-key", fetch_json=fake_fetch)
        series = await client.get_price_history("spy", interval="1day", outputsize=2)
        self.assertEqual(series.symbol, "SPY")
        self.assertEqual(series.interval, "1day")
        self.assertEqual(series.values[0].close, 679.86)
        self.assertIn(
            f"{TWELVE_DATA_BASE_URL}/time_series?symbol=SPY&interval=1day&outputsize=2&apikey=twelve-key",
            captured_urls[0],
        )

    async def test_missing_api_key_fails_fast(self) -> None:
        called = False

        def fake_fetch(url: str) -> object:
            nonlocal called
            called = True
            return {}

        client = TwelveDataClient(None, fetch_json=fake_fetch)
        with self.assertRaises(TwelveDataAPIKeyMissingError):
            await client.get_market_quote("SPX")
        self.assertFalse(called)

    def test_fetch_json_sync_wraps_url_errors(self) -> None:
        client = TwelveDataClient("twelve-key")
        with patch("nycti.twelvedata.client.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(TwelveDataHTTPError):
                client._fetch_json_sync("https://example.com")

    def test_fetch_json_sync_sends_browser_like_headers(self) -> None:
        captured_request = None

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"symbol":"SPX","close":"5234.12"}'

            @property
            def headers(self):
                class _Headers:
                    @staticmethod
                    def get_content_charset():
                        return "utf-8"

                return _Headers()

        def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            nonlocal captured_request
            captured_request = request
            return _Response()

        client = TwelveDataClient("twelve-key")
        with patch("nycti.twelvedata.client.urlopen", side_effect=fake_urlopen):
            client._fetch_json_sync("https://example.com")

        assert captured_request is not None
        self.assertEqual(captured_request.headers["User-agent"], TWELVE_DATA_USER_AGENT)
        self.assertIn("application/json", captured_request.headers["Accept"])
        self.assertEqual(captured_request.headers["Accept-language"], "en-US,en;q=0.9")

    def test_fetch_json_sync_summarizes_cloudflare_error_payload(self) -> None:
        payload = (
            b'{"title":"Error 1010: Access denied","status":403,'
            b'"detail":"The site owner has blocked access based on your browser\\u0027s signature.",'
            b'"error_code":1010}'
        )
        error = HTTPError(
            url="https://example.com",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=BytesIO(payload),
        )
        client = TwelveDataClient("twelve-key")
        with patch("nycti.twelvedata.client.urlopen", side_effect=error):
            with self.assertRaises(TwelveDataHTTPError) as raised:
                client._fetch_json_sync("https://example.com")
        self.assertEqual(
            str(raised.exception),
            "Error 1010: Access denied: The site owner has blocked access based on your browser's signature.",
        )


if __name__ == "__main__":
    unittest.main()
