import unittest
from unittest.mock import patch
from urllib.error import URLError

from nycti.alpaca.client import ALPACA_MARKET_DATA_BASE_URL, AlpacaClient
from nycti.alpaca.formatting import format_stock_snapshot_message
from nycti.alpaca.models import AlpacaAPIKeyMissingError, AlpacaHTTPError


class AlpacaFormattingTests(unittest.TestCase):
    def test_format_stock_snapshot_message_includes_trade_quote_and_change(self) -> None:
        from nycti.alpaca.models import AlpacaBar, AlpacaQuote, AlpacaStockSnapshot, AlpacaTrade

        snapshot = AlpacaStockSnapshot(
            symbol="AAPL",
            latest_trade=AlpacaTrade(price=210.25, timestamp="2026-04-08T20:00:00Z"),
            latest_quote=AlpacaQuote(bid_price=210.2, ask_price=210.3, timestamp="2026-04-08T20:00:00Z"),
            daily_bar=AlpacaBar(close=209.9, timestamp="2026-04-08"),
            previous_daily_bar=AlpacaBar(close=205.0, timestamp="2026-04-07"),
            feed="iex",
        )
        message = format_stock_snapshot_message(snapshot)
        self.assertIn("Alpaca stock snapshot for: AAPL", message)
        self.assertIn("Feed: iex", message)
        self.assertIn("Last trade: $210.2500", message)
        self.assertIn("Bid/ask: $210.2000 / $210.3000", message)
        self.assertIn("Change vs prev close ($205.0000): +5.2500 (+2.56%)", message)


class AlpacaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_stock_snapshot_returns_structured_snapshot(self) -> None:
        captured_urls: list[str] = []

        def fake_fetch(url: str) -> object:
            captured_urls.append(url)
            return {
                "latestTrade": {"p": 171.25, "t": "2026-04-08T20:00:00Z", "s": 100, "x": "V"},
                "latestQuote": {"bp": 171.2, "ap": 171.3, "bs": 3, "as": 2, "t": "2026-04-08T20:00:00Z"},
                "dailyBar": {"c": 170.8, "o": 169.0, "h": 172.0, "l": 168.5, "v": 12345, "t": "2026-04-08"},
                "prevDailyBar": {"c": 168.0, "t": "2026-04-07"},
            }

        client = AlpacaClient(
            "alpaca-id",
            "alpaca-secret",
            fetch_json=fake_fetch,
        )
        snapshot = await client.get_stock_snapshot("nvda")
        self.assertEqual(snapshot.symbol, "NVDA")
        assert snapshot.latest_trade is not None
        self.assertEqual(snapshot.latest_trade.price, 171.25)
        assert snapshot.latest_quote is not None
        self.assertEqual(snapshot.latest_quote.ask_price, 171.3)
        self.assertIn(f"{ALPACA_MARKET_DATA_BASE_URL}/v2/stocks/NVDA/snapshot?feed=iex", captured_urls[0])

    async def test_missing_api_keys_fail_fast(self) -> None:
        called = False

        def fake_fetch(url: str) -> object:
            nonlocal called
            called = True
            return {}

        client = AlpacaClient(None, None, fetch_json=fake_fetch)
        with self.assertRaises(AlpacaAPIKeyMissingError):
            await client.get_stock_snapshot("AAPL")
        self.assertFalse(called)

    def test_fetch_json_sync_wraps_url_errors(self) -> None:
        client = AlpacaClient("alpaca-id", "alpaca-secret")
        with patch("nycti.alpaca.client.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(AlpacaHTTPError):
                client._fetch_json_sync("https://example.com")


if __name__ == "__main__":
    unittest.main()
