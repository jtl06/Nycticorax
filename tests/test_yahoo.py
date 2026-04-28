import unittest

from nycti.yahoo import YahooFinanceClient, YahooFinanceNoExtendedHoursError
from nycti.yahoo.formatting import format_yahoo_extended_hours_message
from nycti.yahoo.models import YahooExtendedHoursQuote


class YahooFinanceClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_extended_hours_quote_returns_latest_postmarket_candle(self) -> None:
        captured_urls: list[str] = []

        def fake_fetch(url: str) -> object:
            captured_urls.append(url)
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "NVDA",
                                "currency": "USD",
                                "exchangeName": "NMS",
                                "exchangeTimezoneName": "America/New_York",
                                "marketState": "POST",
                                "currentTradingPeriod": {
                                    "regular": {
                                        "start": 1_776_758_600,
                                        "end": 1_776_782_800,
                                    }
                                },
                            },
                            "timestamp": [1_776_782_700, 1_776_783_100, 1_776_783_160],
                            "indicators": {
                                "quote": [
                                    {
                                        "close": [200.0, None, 205.5],
                                    }
                                ]
                            },
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        quote = await client.get_extended_hours_quote("nvda")

        self.assertEqual(quote.symbol, "NVDA")
        self.assertEqual(quote.session, "post")
        self.assertEqual(quote.price, 205.5)
        self.assertIn("/v8/finance/chart/NVDA?", captured_urls[0])
        self.assertIn("includePrePost=true", captured_urls[0])

    async def test_get_extended_hours_quote_rejects_regular_session_candle(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "NVDA",
                                "currentTradingPeriod": {
                                    "regular": {
                                        "start": 1_776_758_600,
                                        "end": 1_776_782_800,
                                    }
                                },
                            },
                            "timestamp": [1_776_770_000],
                            "indicators": {"quote": [{"close": [200.0]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        with self.assertRaises(YahooFinanceNoExtendedHoursError):
            await client.get_extended_hours_quote("NVDA")


class YahooFinanceFormattingTests(unittest.TestCase):
    def test_format_yahoo_extended_hours_message_includes_close_delta(self) -> None:
        quote = YahooExtendedHoursQuote(
            symbol="NVDA",
            price=205.5,
            timestamp=1_776_806_400,
            session="post",
            currency="USD",
            exchange_name="NMS",
            timezone_name="America/New_York",
            market_state="POST",
        )

        message = format_yahoo_extended_hours_message(quote, regular_close=200.0)

        self.assertIn("Yahoo Finance extended-hours fallback for: NVDA | NMS", message)
        self.assertIn("After-hours price: USD 205.5000", message)
        self.assertIn("Extended-hours change: +5.5000 (+2.75%) vs Twelve Data close 200.0000", message)


if __name__ == "__main__":
    unittest.main()
