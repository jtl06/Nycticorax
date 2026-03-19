import unittest
from unittest.mock import patch
from urllib.error import URLError

from nycti.sec.client import COMPANY_TICKERS_URL, SUBMISSIONS_URL_TEMPLATE, SecClient
from nycti.sec.formatting import format_latest_filings_message
from nycti.sec.models import SecHTTPError, SecNoFilingsError, SecTickerNotFoundError, SecUserAgentMissingError
from nycti.sec.parser import build_primary_doc_url, normalize_ticker, parse_company_tickers, parse_recent_filings


class SecParserTests(unittest.TestCase):
    def test_normalize_ticker_strips_and_uppercases(self) -> None:
        self.assertEqual(normalize_ticker(" aapl "), "AAPL")

    def test_parse_company_tickers_handles_sec_shape(self) -> None:
        payload = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
        }
        records = parse_company_tickers(payload)
        self.assertIn("AAPL", records)
        self.assertEqual(records["AAPL"].company_name, "Apple Inc.")
        self.assertEqual(records["AAPL"].cik, 320193)

    def test_parse_recent_filings_builds_primary_doc_urls(self) -> None:
        payload = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000010", "0000320193-25-000009"],
                    "filingDate": ["2025-01-31", "2024-11-01"],
                    "form": ["10-Q", "8-K"],
                    "primaryDocument": ["a10q.htm", "a8k.htm"],
                    "primaryDocDescription": ["Quarterly report", "Current report"],
                    "reportDate": ["2025-01-25", "2024-10-30"],
                }
            }
        }
        filings = parse_recent_filings(payload, cik=320193, limit=1)
        self.assertEqual(len(filings), 1)
        self.assertEqual(
            filings[0].primary_doc_url,
            build_primary_doc_url(cik=320193, accession_number="0000320193-25-000010", primary_document="a10q.htm"),
        )
        self.assertEqual(filings[0].report_date, "2025-01-25")
        self.assertEqual(filings[0].description, "Quarterly report")

    def test_format_latest_filings_message_includes_summary(self) -> None:
        payload = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000010"],
                    "filingDate": ["2025-01-31"],
                    "form": ["10-Q"],
                    "primaryDocument": ["a10q.htm"],
                }
            }
        }
        filings = parse_recent_filings(payload, cik=320193, limit=5)
        from nycti.sec.models import SecLatestFilings

        message = format_latest_filings_message(
            SecLatestFilings(ticker="AAPL", company_name="Apple Inc.", cik=320193, filings=filings)
        )
        self.assertIn("Latest SEC filings for Apple Inc. (AAPL)", message)
        self.assertIn("earnings-related form", message)


class SecClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_filings_returns_summary(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            },
            SUBMISSIONS_URL_TEMPLATE.format(cik=320193): {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000010", "0000320193-24-000111"],
                        "filingDate": ["2025-01-31", "2024-11-01"],
                        "form": ["10-Q", "8-K"],
                        "primaryDocument": ["a10q.htm", "a8k.htm"],
                        "reportDate": ["2025-01-25", "2024-10-30"],
                    }
                }
            },
        }
        requested: list[str] = []

        def fake_fetch(url: str) -> object:
            requested.append(url)
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        result = await client.latest_filings(" aapl ", limit=1)

        self.assertEqual(requested, [COMPANY_TICKERS_URL, SUBMISSIONS_URL_TEMPLATE.format(cik=320193)])
        self.assertEqual(result.ticker, "AAPL")
        self.assertEqual(result.company_name, "Apple Inc.")
        self.assertEqual(len(result.filings), 1)
        self.assertEqual(result.filings[0].form, "10-Q")

    async def test_latest_filings_from_text_uses_ticker_in_query(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            },
            SUBMISSIONS_URL_TEMPLATE.format(cik=320193): {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-25-000010"],
                        "filingDate": ["2025-01-31"],
                        "form": ["10-Q"],
                        "primaryDocument": ["a10q.htm"],
                    }
                }
            },
        }

        def fake_fetch(url: str) -> object:
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        result = await client.latest_filings_from_text("latest aapl earnings", limit=1)
        self.assertEqual(result.ticker, "AAPL")
        self.assertEqual(len(result.filings), 1)

    async def test_latest_filings_from_text_matches_company_name_when_no_ticker(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 723125, "ticker": "MU", "title": "Micron Technology, Inc."},
                "1": {"cik_str": 1431959, "ticker": "FOR", "title": "Forestar Group Inc."},
            },
            SUBMISSIONS_URL_TEMPLATE.format(cik=723125): {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000723125-25-000010"],
                        "filingDate": ["2025-01-08"],
                        "form": ["8-K"],
                        "primaryDocument": ["mu-earnings.htm"],
                    }
                }
            },
        }

        def fake_fetch(url: str) -> object:
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        result = await client.latest_filings_from_text("what is the latest er for micron", limit=1)
        self.assertEqual(result.ticker, "MU")
        self.assertEqual(result.company_name, "Micron Technology, Inc.")

    async def test_find_matching_companies_prefers_company_name_match_over_stopword_ticker(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 723125, "ticker": "MU", "title": "Micron Technology, Inc."},
                "1": {"cik_str": 1431959, "ticker": "FOR", "title": "Forestar Group Inc."},
            },
        }

        def fake_fetch(url: str) -> object:
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        matches = await client.find_matching_companies("what is the latest er for micron", limit=2)
        self.assertEqual([match.ticker for match in matches], ["MU"])

    async def test_missing_user_agent_fails_fast(self) -> None:
        called = False

        def fake_fetch(url: str) -> object:
            nonlocal called
            called = True
            return {}

        client = SecClient(None, fetch_json=fake_fetch)
        with self.assertRaises(SecUserAgentMissingError):
            await client.latest_filings("AAPL")
        self.assertFalse(called)

    async def test_unknown_ticker_raises_clear_error(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            },
        }

        def fake_fetch(url: str) -> object:
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        with self.assertRaises(SecTickerNotFoundError):
            await client.latest_filings("ZZZZ")

    async def test_no_filings_raises_clear_error(self) -> None:
        responses = {
            COMPANY_TICKERS_URL: {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            },
            SUBMISSIONS_URL_TEMPLATE.format(cik=320193): {
                "filings": {"recent": {"accessionNumber": [], "filingDate": [], "form": [], "primaryDocument": []}}
            },
        }

        def fake_fetch(url: str) -> object:
            return responses[url]

        client = SecClient("Nycti/1.0 (ops@example.com)", fetch_json=fake_fetch)
        with self.assertRaises(SecNoFilingsError):
            await client.latest_filings("AAPL")

    def test_fetch_json_sync_wraps_url_errors(self) -> None:
        client = SecClient("Nycti/1.0 (ops@example.com)")
        with patch("nycti.sec.client.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(SecHTTPError):
                client._fetch_json_sync("https://example.com")


if __name__ == "__main__":
    unittest.main()
