import asyncio
import json
from types import SimpleNamespace
import unittest

from nycti.chat.tools.content import ContentToolMixin, default_extract_query
from nycti.chat.tools.market_pages import market_page_info
from nycti.chat.tools.market_page_data import MarketPageObservation
from nycti.chat.tools.parsing import parse_extract_url_arguments
from nycti.tavily.models import TavilyExtractResponse, TavilyExtractResult


class MarketPageTests(unittest.TestCase):
    def test_direct_success_and_validation_fallback(self):
        async def run():
            url = "https://stockanalysis.com/stocks/mu/"
            calls = []

            async def extract(**kwargs):
                calls.append(kwargs)
                return TavilyExtractResponse(url, [TavilyExtractResult(url, "Fallback evidence")])

            async def direct(_):
                return MarketPageObservation(url, "MU", "2026-10-30T00:00:00+00:00")

            tool = ContentToolMixin()
            tool.tavily_client = SimpleNamespace(extract=extract)
            tool.market_page_client = SimpleNamespace(get=direct)
            self.assertTrue((await tool._execute_extract_url_tool(url=url, query=None)).startswith("Direct market page"))
            self.assertEqual([], calls)

            async def broken(_):
                raise ValueError("layout changed")

            tool.market_page_client = SimpleNamespace(get=broken)
            self.assertIn("Fallback evidence", await tool._execute_extract_url_tool(url=url, query=None))
            self.assertEqual(1, len(calls))
        asyncio.run(run())

    def test_symbol_lookup_routes_and_existing_exact_url(self):
        cases = (
            ({"market_source": "stockscan", "symbol": "nvda"},
             "https://stockscan.io/stocks/NVDA/price-history"),
            ({"market_source": "stockanalysis", "symbol": "MU", "url": None},
             "https://stockanalysis.com/stocks/mu/"),
            ({"url": "https://stockanalysis.com/etf/spy/", "market_source": None, "symbol": None},
             "https://stockanalysis.com/etf/spy/"),
        )
        for arguments, url in cases:
            with self.subTest(arguments=arguments):
                result = parse_extract_url_arguments(json.dumps(arguments))
                self.assertIsNotNone(result)
                self.assertEqual(url, result.url)

    def test_lookup_rejects_ambiguous_or_unsupported_inputs(self):
        for arguments in (
            {"market_source": "unknown", "symbol": "NVDA"},
            {"market_source": "stockscan", "symbol": "../NVDA"},
            {"market_source": "stockscan", "symbol": "NVDA?x=1"},
            {"market_source": "stockanalysis", "symbol": "NVDA", "url": "https://example.com"},
            {"market_source": "stockanalysis", "symbol": 12},
            {"symbol": "NVDA"}, {"market_source": "stockscan"},
            {"market_source": "stockanalysis", "symbol": "BRK.B"},
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNone(parse_extract_url_arguments(json.dumps(arguments)))

    def test_focus_is_scoped_to_real_source_routes(self):
        for url in ("https://stockscan.io/stocks/NVDA/price-history",
                    "https://stockanalysis.com/stocks/mu/", "https://stockanalysis.com/etf/spy/"):
            self.assertIsNotNone(default_extract_query(url))
        for url in ("https://stockanalysis.com.evil.invalid/stocks/mu/",
                    "https://stockanalysis.com@evil.invalid/stocks/mu/",
                    "https://stockanalysis.com:bad/stocks/mu/", "https://stockanalysis.com/p/private",
                    "https://stockscan.io/stocks/NVDA/forecast"):
            self.assertIsNone(market_page_info(url))

    def test_extraction_preserves_basis_and_timestamp_without_becoming_quote_feed(self):
        async def run():
            for source, symbol, evidence, expected_focus, expected_note in (
                ("stockscan", "NVDA", "Split/dividend-adjusted ATH $150 on June 1; coverage begins 2016.",
                 "adjustment basis", "not a complete candle series"),
                ("stockanalysis", "MU", "Regular close $100; AH $102 at 6:05 PM ET, +2% vs close.",
                 "percentage-change comparison basis", "not a streaming feed"),
            ):
                payload = parse_extract_url_arguments(json.dumps({"market_source": source, "symbol": symbol}))
                calls = []

                async def extract(**kwargs):
                    calls.append(kwargs)
                    return TavilyExtractResponse(payload.url, [TavilyExtractResult(payload.url, evidence)])

                tool = ContentToolMixin()
                tool.tavily_client = SimpleNamespace(extract=extract)
                result = await tool._execute_extract_url_tool(url=payload.url, query="requested detail")
                self.assertEqual(1, len(calls))
                self.assertEqual(payload.url, calls[0]["url"])
                self.assertEqual(5, calls[0]["chunks_per_source"])
                self.assertIn(expected_focus, calls[0]["query"])
                self.assertIn("requested detail", calls[0]["query"])
                self.assertIn(evidence, result)
                self.assertIn(expected_note, result)
                self.assertTrue(result.startswith("Tavily extract for:"))
        asyncio.run(run())

    def test_empty_page_is_not_reported_as_usable_market_evidence(self):
        async def extract(**kwargs):
            return TavilyExtractResponse(kwargs["url"], [])

        tool = ContentToolMixin()
        tool.tavily_client = SimpleNamespace(extract=extract)
        result = asyncio.run(tool._execute_extract_url_tool(
            url="https://stockanalysis.com/stocks/mu/", query=None))
        self.assertTrue(result.startswith("No extractable content"))
        self.assertNotIn("Market-page interpretation", result)


if __name__ == "__main__":
    unittest.main()
