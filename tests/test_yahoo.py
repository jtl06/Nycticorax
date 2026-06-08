import json
import unittest

from nycti.yahoo import YahooFinanceClient, YahooFinanceNoExtendedHoursError
from nycti.yahoo.formatting import format_yahoo_extended_hours_message
from nycti.yahoo.models import YahooExtendedHoursQuote


class YahooFinanceClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_extended_hours_quote_prefers_page_overnight_price(self) -> None:
        captured_urls: list[str] = []
        body = {
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "SPY",
                        "currency": "USD",
                        "fullExchangeName": "NYSEArca",
                        "exchangeTimezoneName": "America/New_York",
                        "overnightMarketPrice": {"raw": 737.76, "fmt": "737.76"},
                        "overnightMarketTime": {"raw": 1_780_899_941, "fmt": "6:25AM UTC"},
                        "postMarketPrice": {"raw": 735.01, "fmt": "735.01"},
                        "postMarketTime": {"raw": 1_780_703_999, "fmt": "7:59PM EDT"},
                    }
                ]
            }
        }
        outer = {"body": json.dumps(body)}
        page_text = (
            '<html><body><script type="application/json" '
            'data-url="https://query1.finance.yahoo.com/v7/finance/quote?symbols=SPY&overnightPrice=true">'
            f"{json.dumps(outer)}"
            "</script></body></html>"
        )

        def fake_fetch_text(url: str) -> str:
            captured_urls.append(url)
            return page_text

        def fake_fetch_json(url: str) -> object:
            raise AssertionError("chart endpoint should not be called when page quote data is usable")

        client = YahooFinanceClient(fetch_json=fake_fetch_json, fetch_text=fake_fetch_text)
        quote = await client.get_extended_hours_quote("spy")

        self.assertEqual(captured_urls, ["https://finance.yahoo.com/quote/SPY/"])
        self.assertEqual(quote.symbol, "SPY")
        self.assertEqual(quote.session, "overnight")
        self.assertEqual(quote.price, 737.76)
        self.assertEqual(quote.timestamp, 1_780_899_941)
        self.assertEqual(quote.exchange_name, "NYSEArca")

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
        self.assertIn("https://query2.finance.yahoo.com/v8/finance/chart/NVDA?", captured_urls[0])
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

    async def test_get_extended_hours_quote_rejects_closed_market_stale_postmarket_candle(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "NVDA",
                                "marketState": "CLOSED",
                                "currentTradingPeriod": {
                                    "regular": {
                                        "start": 1_776_758_600,
                                        "end": 1_776_782_800,
                                    }
                                },
                            },
                            "timestamp": [1_776_783_160],
                            "indicators": {"quote": [{"close": [205.5]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        with self.assertRaises(YahooFinanceNoExtendedHoursError):
            await client.get_extended_hours_quote("NVDA")

    async def test_get_extended_hours_quote_rejects_stale_candle_outside_current_periods(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "SPY",
                                "currentTradingPeriod": {
                                    "pre": {"start": 1_780_905_600, "end": 1_780_925_400},
                                    "regular": {"start": 1_780_925_400, "end": 1_780_948_800},
                                    "post": {"start": 1_780_948_800, "end": 1_780_963_200},
                                },
                            },
                            "timestamp": [1_780_703_999],
                            "indicators": {"quote": [{"close": [735.01]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        with self.assertRaises(YahooFinanceNoExtendedHoursError):
            await client.get_extended_hours_quote("SPY")

    async def test_get_extended_hours_quote_accepts_candle_inside_current_pre_period(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "SPY",
                                "currentTradingPeriod": {
                                    "pre": {"start": 1_780_905_600, "end": 1_780_925_400},
                                    "regular": {"start": 1_780_925_400, "end": 1_780_948_800},
                                    "post": {"start": 1_780_948_800, "end": 1_780_963_200},
                                },
                            },
                            "timestamp": [1_780_906_000],
                            "indicators": {"quote": [{"close": [736.25]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        quote = await client.get_extended_hours_quote("SPY")

        self.assertEqual(quote.session, "pre")
        self.assertEqual(quote.price, 736.25)

    async def test_get_extended_hours_quote_accepts_prepre_market_state_without_regular_bounds(self) -> None:
        def fake_fetch(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "NVDA",
                                "marketState": "PREPRE",
                                "exchangeTimezoneName": "America/New_York",
                            },
                            "timestamp": [1_776_900_000],
                            "indicators": {"quote": [{"close": [207.25]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(fetch_json=fake_fetch)
        quote = await client.get_extended_hours_quote("NVDA")

        self.assertEqual(quote.session, "pre")
        self.assertEqual(quote.price, 207.25)
        self.assertEqual(quote.market_state, "PREPRE")


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

    def test_format_yahoo_extended_hours_message_labels_overnight(self) -> None:
        quote = YahooExtendedHoursQuote(
            symbol="SPY",
            price=737.76,
            timestamp=1_780_899_941,
            session="overnight",
            currency="USD",
            exchange_name="NYSEArca",
            timezone_name="America/New_York",
        )

        message = format_yahoo_extended_hours_message(quote, regular_close=757.09)

        self.assertIn("Overnight price: USD 737.7600", message)


if __name__ == "__main__":
    unittest.main()
