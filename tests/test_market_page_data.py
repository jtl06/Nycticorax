import asyncio
from datetime import datetime, timezone
import unittest
from threading import Barrier

from nycti.chat.tools.market_page_client import MarketPageClient, render_market_observation
from nycti.chat.tools.market_page_data import MarketPageDataError, parse_market_page
from nycti.yahoo.quote_cache import QuotePageCache


URL = "https://stockanalysis.com/stocks/nvda/"
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
QUOTE = """<h1>NVIDIA (NVDA)</h1><div>NASDAQ: NVDA · Real-Time Price · USD</div>
<div>100.00</div><div>+10.00 (+11.11%)</div><div>At close:</div>
<div>Oct 29, 2026, 4:00 PM EDT</div><div>102.00</div><div>+2.00 (+2.00%)</div>
<div>After-hours:</div><div>Oct 29, 2026, 7:59 PM EDT</div><nav></nav>
<table><tr><td>Previous Close</td><td>90.00</td></tr></table>"""
HISTORY = """<div data-symboldetail='{&quot;symbol&quot;:&quot;NVDA&quot;,
&quot;priceHistory&quot;:[{&quot;name&quot;:&quot;highest&quot;,
&quot;date&quot;:&quot;2026-10-06&quot;,&quot;value&quot;:150}]}'>
adjusted for splits and dividends; latest closing stock price as of October 29, 2026, is $100.
all-time high stock price is <b>$150</b>, occurred on October 06, 2026.</div>
<table><tr><td>Oct 29, 2026</td><td>105</td><td>99</td><td>6</td><td>100</td><td>1%</td></tr></table>"""


class MarketPageDataTests(unittest.TestCase):
    def test_warmup_only_fetches_both_policies_in_parallel(self):
        async def run():
            barrier = Barrier(2)
            calls = []

            def fetch(url, limit):
                calls.append(url)
                self.assertTrue(url.endswith("/robots.txt"))
                barrier.wait(timeout=2)
                return "User-agent: *\nAllow: /"

            client = MarketPageClient(fetch_text=fetch)
            await client.warmup()
            await client._policy("stockanalysis.com")
            await client._policy("stockscan.io")
            self.assertCountEqual(calls, ["https://stockanalysis.com/robots.txt", "https://stockscan.io/robots.txt"])
        asyncio.run(run())

    def test_daily_policy_expiry_never_reuses_permission_on_failure(self):
        async def run():
            clock = [0.0]
            calls = []

            def fetch(url, limit):
                calls.append(url)
                if len(calls) > 1:
                    raise OSError("policy unavailable")
                return "User-agent: *\nAllow: /"

            client = MarketPageClient(fetch_text=fetch)
            client._robots_cache = QuotePageCache(clock=lambda: clock[0], ttl_seconds=86400)
            await client._policy("stockanalysis.com")
            clock[0] = 86399
            await client._policy("stockanalysis.com")
            self.assertEqual(1, len(calls))
            clock[0] = 86400
            for _ in range(2):
                with self.assertRaises(OSError):
                    await client.get(URL)
            self.assertEqual(3, len(calls))
            self.assertTrue(all(url.endswith("robots.txt") for url in calls))
        asyncio.run(run())

    def test_distinct_pages_fetch_in_parallel_and_share_policy(self):
        async def run():
            barrier = Barrier(2)
            calls = []

            def fetch(url, limit):
                calls.append(url)
                if url.endswith("robots.txt"):
                    return "User-agent: *\nAllow: /"
                barrier.wait(timeout=2)
                return QUOTE if "/nvda/" in url else QUOTE.replace("NVDA", "MU")

            client = MarketPageClient(fetch_text=fetch)
            a, b = await asyncio.gather(client.get(URL), client.get(URL.replace("nvda", "mu")))
            self.assertEqual(("NVDA", "MU"), (a.symbol, b.symbol))
            self.assertEqual(1, sum(url.endswith("robots.txt") for url in calls))
        asyncio.run(run())

    def test_quote_basis_and_staleness(self):
        result = parse_market_page(URL, QUOTE, now=NOW)
        self.assertEqual(90, result.regular.reference_price)
        self.assertEqual(100, result.extended.reference_price)
        self.assertEqual("after-hours", result.extended.session)
        self.assertIn("not a confirmed live price", render_market_observation(result, "notes"))

    def test_invalid_quotes_fail_closed(self):
        for html in (QUOTE.replace("NASDAQ: NVDA", "NASDAQ: AMD"),
                     QUOTE.replace("+2.00%", "+20.00%"),
                     QUOTE.replace("7:59 PM EDT", "7:59 PM EST"),
                     QUOTE.replace("Oct 29, 2026, 7:59 PM EDT", "unknown")):
            with self.subTest(html=html), self.assertRaises(MarketPageDataError):
                parse_market_page(URL, html, now=NOW)

    def test_reported_history_is_labeled_not_full_history(self):
        url = "https://stockscan.io/stocks/NVDA/price-history"
        result = parse_market_page(url, HISTORY, now=NOW)
        self.assertEqual(150, result.reported_ath)
        self.assertEqual("split/dividend adjusted", result.adjustment_basis)
        self.assertEqual(1, len(result.history_rows))
        self.assertIn("not full coverage", render_market_observation(result, "notes"))
        with self.assertRaises(MarketPageDataError):
            parse_market_page(url, HISTORY.replace("value&quot;:150", "value&quot;:151"), now=NOW)
        with self.assertRaises(MarketPageDataError):
            parse_market_page("https://evil.invalid/", HISTORY, now=NOW)

    def test_client_singleflight_cache_and_robots(self):
        async def run():
            calls = []

            def fetch(url, limit):
                calls.append(url)
                return "User-agent: *\nAllow: /" if url.endswith("robots.txt") else QUOTE

            client = MarketPageClient(fetch_text=fetch)
            a, b = await asyncio.gather(client.get(URL), client.get(URL))
            self.assertEqual(a, b)
            await client.get(URL)
            self.assertEqual(2, len(calls))
            denied = MarketPageClient(fetch_text=lambda *_: "User-agent: *\nDisallow: /")
            with self.assertRaises(MarketPageDataError):
                await denied.get(URL)
        asyncio.run(run())
