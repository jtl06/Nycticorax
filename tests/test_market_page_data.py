import asyncio
from datetime import datetime, timezone
import unittest

from nycti.chat.tools.market_page_client import MarketPageClient, render_market_observation
from nycti.chat.tools.market_page_data import MarketPageDataError, parse_market_page


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
