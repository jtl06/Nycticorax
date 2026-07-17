import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from nycti.yahoo import YahooFinanceClient, YahooFinanceNoExtendedHoursError
from nycti.yahoo.annual import format_annual_performance
from nycti.yahoo.formatting import (
    format_yahoo_extended_hours_message,
    format_yahoo_market_snapshot_message,
)
from nycti.yahoo.models import YahooExtendedHoursQuote, YahooMarketSnapshot


class YahooFinanceClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_annual_performance_calculates_price_and_distributions(self) -> None:
        captured_urls: list[str] = []
        timestamps = [
            int(_ny_datetime(2019, 12, 31, 16, 0).timestamp()),
            int(_ny_datetime(2020, 1, 2, 16, 0).timestamp()),
            int(_ny_datetime(2020, 12, 31, 16, 0).timestamp()),
            int(_ny_datetime(2021, 12, 31, 16, 0).timestamp()),
            int(_ny_datetime(2022, 6, 10, 16, 0).timestamp()),
        ]

        def fake_fetch(url: str) -> object:
            captured_urls.append(url)
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "^GSPC",
                                "currency": "USD",
                                "exchangeTimezoneName": "America/New_York",
                            },
                            "timestamp": timestamps,
                            "indicators": {"quote": [{"close": [100.0, 101.0, 110.0, 99.0, 105.0]}]},
                            "events": {
                                "dividends": {
                                    "a": {"date": timestamps[1], "amount": 5.0},
                                    "b": {"date": timestamps[3], "amount": 7.0},
                                    "c": {"date": timestamps[4], "amount": 3.0},
                                }
                            },
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(
            fetch_json=fake_fetch,
            now=lambda: _ny_datetime(2022, 6, 14, 12, 0),
        )
        performance = await client.get_annual_performance("SPX", start_year=2020)

        self.assertIn("/chart/%5EGSPC?", captured_urls[0])
        self.assertEqual(performance.requested_symbol, "SPX")
        self.assertEqual(performance.symbol, "^GSPC")
        self.assertEqual(len(performance.years), 3)
        self.assertAlmostEqual(performance.years[0].price_change_percent, 10.0)
        self.assertAlmostEqual(performance.years[0].distribution_percent_of_start, 5.0)
        self.assertAlmostEqual(performance.years[1].price_change_percent, -10.0)
        self.assertTrue(performance.years[2].partial_year)
        rendered = format_annual_performance(performance)
        self.assertIn("2020: price +10.00%", rendered)
        self.assertIn("Source: https://finance.yahoo.com/quote/^GSPC/history/", rendered)

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

        client = YahooFinanceClient(
            fetch_json=fake_fetch_json,
            fetch_text=fake_fetch_text,
            now=lambda: _ny_datetime(2026, 6, 8, 21, 0),
        )
        quote = await client.get_extended_hours_quote("spy")

        self.assertEqual(captured_urls, ["https://finance.yahoo.com/quote/SPY/"])
        self.assertEqual(quote.symbol, "SPY")
        self.assertEqual(quote.session, "overnight")
        self.assertEqual(quote.price, 737.76)
        self.assertEqual(quote.timestamp, 1_780_899_941)
        self.assertEqual(quote.exchange_name, "NYSEArca")

    async def test_get_market_snapshot_extracts_regular_session_valuation(self) -> None:
        captured_urls: list[str] = []
        body = {
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "AAPL",
                        "currency": "USD",
                        "fullExchangeName": "NasdaqGS",
                        "exchangeTimezoneName": "America/New_York",
                        "marketState": "REGULAR",
                        "regularMarketPrice": {"raw": 333.74},
                        "regularMarketTime": {"raw": 1_784_318_401},
                        "marketCap": {"raw": 4_901_757_779_968},
                        "sharesOutstanding": {"raw": 14_687_356_000},
                        "impliedSharesOutstanding": {"raw": 14_700_000_000},
                    }
                ]
            }
        }
        client = YahooFinanceClient(
            fetch_json=lambda url: {},
            fetch_text=lambda url: captured_urls.append(url) or _quote_page_html("AAPL", body),
            now=lambda: _ny_datetime(2026, 7, 17, 13, 0),
        )

        snapshot = await client.get_market_snapshot("aapl")

        self.assertEqual(captured_urls, ["https://finance.yahoo.com/quote/AAPL/"])
        self.assertEqual(snapshot.symbol, "AAPL")
        self.assertEqual(snapshot.market_cap, 4_901_757_779_968)
        self.assertEqual(snapshot.shares_outstanding, 14_687_356_000)
        self.assertEqual(snapshot.implied_shares_outstanding, 14_700_000_000)
        self.assertIsNone(snapshot.extended_price)

    async def test_get_extended_hours_quote_uses_postmarket_when_page_state_is_post(self) -> None:
        body = {
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "SMCI",
                        "currency": "USD",
                        "fullExchangeName": "NasdaqGS",
                        "exchangeTimezoneName": "America/New_York",
                        "marketState": "POST",
                        "regularMarketPrice": {"raw": 29.27, "fmt": "29.27"},
                        "regularMarketTime": {"raw": 1_781_121_600, "fmt": "4:00PM EDT"},
                        "preMarketPrice": {"raw": 35.52, "fmt": "35.52"},
                        "preMarketTime": {"raw": 1_781_098_199, "fmt": "9:29AM EDT"},
                        "postMarketPrice": {"raw": 29.13, "fmt": "29.13"},
                        "postMarketTime": {"raw": 1_781_128_103, "fmt": "5:48PM EDT"},
                    }
                ]
            }
        }
        page_text = _quote_page_html("SMCI", body)

        client = YahooFinanceClient(
            fetch_json=lambda url: (_ for _ in ()).throw(AssertionError("chart endpoint should not be called")),
            fetch_text=lambda url: page_text,
            now=lambda: _ny_datetime(2026, 6, 10, 17, 50),
        )
        quote = await client.get_extended_hours_quote("SMCI")

        self.assertEqual(quote.session, "post")
        self.assertEqual(quote.price, 29.13)
        self.assertEqual(quote.timestamp, 1_781_128_103)
        self.assertEqual(quote.market_state, "POST")
        self.assertEqual(quote.regular_price, 29.27)
        self.assertEqual(quote.regular_timestamp, 1_781_121_600)

    async def test_get_extended_hours_quote_rejects_page_regular_state_with_stale_extended_fields(self) -> None:
        body = {
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "SMCI",
                        "marketState": "REGULAR",
                        "preMarketPrice": {"raw": 35.52, "fmt": "35.52"},
                        "preMarketTime": {"raw": 1_781_098_199, "fmt": "9:29AM EDT"},
                    }
                ]
            }
        }
        page_text = _quote_page_html("SMCI", body)

        def fake_fetch_json(url: str) -> object:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "SMCI", "marketState": "REGULAR"},
                            "timestamp": [1_781_100_000],
                            "indicators": {"quote": [{"close": [29.27]}]},
                        }
                    ],
                    "error": None,
                }
            }

        client = YahooFinanceClient(
            fetch_json=fake_fetch_json,
            fetch_text=lambda url: page_text,
            now=lambda: _ny_datetime(2026, 6, 10, 14, 0),
        )
        with self.assertRaises(YahooFinanceNoExtendedHoursError):
            await client.get_extended_hours_quote("SMCI")

    async def test_get_extended_hours_quote_uses_current_time_when_page_state_is_stale(self) -> None:
        body = {
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "SMCI",
                        "marketState": "REGULAR",
                        "exchangeTimezoneName": "America/New_York",
                        "preMarketPrice": {"raw": 35.52, "fmt": "35.52"},
                        "preMarketTime": {"raw": 1_781_098_199, "fmt": "9:29AM EDT"},
                        "postMarketPrice": {"raw": 29.13, "fmt": "29.13"},
                        "postMarketTime": {"raw": 1_781_128_103, "fmt": "5:48PM EDT"},
                    }
                ]
            }
        }
        page_text = _quote_page_html("SMCI", body)

        client = YahooFinanceClient(
            fetch_json=lambda url: (_ for _ in ()).throw(AssertionError("chart endpoint should not be called")),
            fetch_text=lambda url: page_text,
            now=lambda: _ny_datetime(2026, 6, 10, 17, 50),
        )
        quote = await client.get_extended_hours_quote("SMCI")

        self.assertEqual(quote.session, "post")
        self.assertEqual(quote.price, 29.13)
        self.assertEqual(quote.market_state, "REGULAR")

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

    def test_format_yahoo_extended_hours_prefers_same_page_regular_close(self) -> None:
        quote = YahooExtendedHoursQuote(
            symbol="SPCX",
            price=166.8341,
            timestamp=1_781_308_799,
            session="post",
            currency="USD",
            exchange_name="NMS",
            timezone_name="America/New_York",
            regular_price=160.95,
            regular_previous_close=135.0,
            regular_change=25.95,
            regular_percent_change=19.22,
        )

        message = format_yahoo_extended_hours_message(quote, regular_close=152.96)

        self.assertIn("Regular close (Yahoo): USD 160.9500 +25.9500 (+19.22%)", message)
        self.assertIn(
            "Extended-hours change: +5.8841 (+3.66%) vs Yahoo regular close 160.9500",
            message,
        )
        self.assertIn(
            "Provider conflict: Twelve Data close 152.9600 differs from Yahoo regular close 160.9500",
            message,
        )

    def test_format_yahoo_market_snapshot_exposes_compact_valuation_inputs(self) -> None:
        snapshot = YahooMarketSnapshot(
            symbol="AAPL",
            currency="USD",
            exchange_name="NasdaqGS",
            timezone_name="America/New_York",
            regular_timestamp=1_784_318_401,
            market_cap=4_901_757_779_968,
            shares_outstanding=14_687_356_000,
        )

        message = format_yahoo_market_snapshot_message(snapshot)

        self.assertIn("Yahoo Finance public-company valuation for: AAPL | NasdaqGS", message)
        self.assertIn("Market cap (regular-price basis): USD 4.9018T", message)
        self.assertIn("Shares outstanding: 14.6874B", message)
        self.assertIn("Valuation quote time:", message)


def _quote_page_html(symbol: str, body: dict[str, object]) -> str:
    outer = {"body": json.dumps(body)}
    return (
        '<html><body><script type="application/json" '
        f'data-url="https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}&overnightPrice=true">'
        f"{json.dumps(outer)}"
        "</script></body></html>"
    )


def _ny_datetime(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


if __name__ == "__main__":
    unittest.main()
