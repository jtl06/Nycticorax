import asyncio
from datetime import datetime
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from nycti.yahoo.client import YahooFinanceClient, YahooFinanceDataError
from nycti.yahoo.quote_cache import QuotePageCache


class QuoteCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_and_same_symbol_single_flight(self):
        now = [0.0]
        cache = QuotePageCache(clock=lambda: now[0])
        calls = 0
        gate = asyncio.Event()

        async def load():
            nonlocal calls
            calls += 1
            await gate.wait()
            return {"price": calls}

        tasks = [asyncio.create_task(cache.get("NVDA", load)) for _ in range(10)]
        gate.set()
        self.assertEqual(await asyncio.gather(*tasks), [{"price": 1}] * 10)
        now[0] = 14.99
        self.assertEqual(await cache.get("NVDA", load), {"price": 1})
        now[0] = 15
        self.assertEqual(await cache.get("NVDA", load), {"price": 2})

    async def test_cancellation_does_not_cancel_another_waiter(self):
        cache = QuotePageCache()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"price": 10}

        first = asyncio.create_task(cache.get("AMD", load))
        await entered.wait()
        second = asyncio.create_task(cache.get("AMD", load))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, {"price": 10})
        self.assertEqual(calls, 1)
        self.assertFalse(cache._pending)

    async def test_failures_shared_but_not_cached(self):
        cache = QuotePageCache()
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            raise ValueError("offline failure")

        results = await asyncio.gather(*(cache.get("MU", load) for _ in range(4)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ValueError) for result in results))
        self.assertEqual(calls, 1)
        with self.assertRaises(ValueError):
            await cache.get("MU", load)
        self.assertEqual(calls, 2)
        self.assertFalse(cache._values)
        self.assertFalse(cache._pending)

    async def test_capacity_lru_and_distinct_symbol_concurrency(self):
        cache = QuotePageCache()
        active = peak = 0

        async def load():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {}

        with patch("nycti.yahoo.quote_cache.QUOTE_CACHE_CAPACITY", 2):
            await asyncio.gather(*(cache.get(str(i), load) for i in range(5)))
            self.assertEqual(peak, 2)
            self.assertEqual(len(cache._values), 2)
            await cache.get("3", load)
            await cache.get("5", load)
            self.assertEqual(set(cache._values), {"3", "5"})

    async def test_slow_fetch_does_not_extend_freshness(self):
        now = [0.0]
        cache = QuotePageCache(clock=lambda: now[0])

        async def load():
            now[0] += 16
            return {}

        await cache.get("SPY", load)
        self.assertFalse(cache._values)


def quote_page(symbol="NVDA"):
    tz = ZoneInfo("America/New_York")
    result = {
        "symbol": symbol, "currency": "USD", "exchangeTimezoneName": "America/New_York",
        "regularMarketPrice": 100, "regularMarketPreviousClose": 99,
        "overnightMarketPrice": 102, "overnightMarketTime": int(datetime(2026, 10, 14, 3, 59, tzinfo=tz).timestamp()),
        "preMarketPrice": 103, "preMarketTime": int(datetime(2026, 10, 14, 4, 0, tzinfo=tz).timestamp()),
    }
    outer = {"body": json.dumps({"quoteResponse": {"result": [result]}})}
    return ('<script type="application/json" data-url="https://query1.finance.yahoo.com/v7/finance/quote?symbols=NVDA">'
            + json.dumps(outer) + '</script>')


class YahooCacheIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_paths_share_normalized_symbol_and_reselect_session(self):
        calls = []
        now = [datetime(2026, 10, 14, 3, 59, 59, tzinfo=ZoneInfo("America/New_York"))]

        def fetch(url):
            calls.append(url)
            return quote_page()

        client = YahooFinanceClient(fetch_text=fetch, now=lambda: now[0])
        snapshot, extended = await asyncio.gather(client.get_market_snapshot(" nvda "), client.get_extended_hours_quote("NVDA"))
        self.assertEqual(snapshot.extended_price, extended.price)
        self.assertEqual(extended.session, "overnight")
        now[0] = now[0].replace(hour=4, minute=0, second=1)
        new = await client.get_market_snapshot("NVDA")
        self.assertEqual(new.extended_session, "pre")
        self.assertEqual(new.extended_price, 103)
        self.assertEqual(new.regular_price, 100)
        self.assertEqual(len(calls), 1)

    async def test_bad_pages_and_wrong_symbols_are_not_cached(self):
        for page in ("broken html", quote_page("AMD")):
            calls = []

            def fetch(url):
                calls.append(url)
                return page

            client = YahooFinanceClient(fetch_text=fetch)
            for _ in range(2):
                with self.assertRaises(YahooFinanceDataError):
                    await client.get_market_snapshot("NVDA")
            self.assertEqual(len(calls), 2)
