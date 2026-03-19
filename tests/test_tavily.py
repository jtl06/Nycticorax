import unittest
from unittest.mock import patch
from urllib.error import URLError

from nycti.tavily.client import TAVILY_SEARCH_URL, TavilyClient
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyHTTPError


class TavilyFormattingTests(unittest.TestCase):
    def test_format_search_message_handles_empty_results(self) -> None:
        from nycti.tavily.models import TavilySearchResponse

        message = format_tavily_search_message(TavilySearchResponse(query="test", results=[]))
        self.assertIn("No web results found", message)

    def test_format_search_message_includes_title_url_and_snippet(self) -> None:
        from nycti.tavily.models import TavilySearchResponse, TavilySearchResult

        response = TavilySearchResponse(
            query="apple earnings",
            results=[
                TavilySearchResult(
                    title="Apple Investor Relations",
                    url="https://investor.apple.com",
                    content="Apple reported quarterly earnings and provided guidance.",
                )
            ],
        )
        message = format_tavily_search_message(response)
        self.assertIn("Tavily web results for: apple earnings", message)
        self.assertIn("Apple Investor Relations", message)
        self.assertIn("https://investor.apple.com", message)


class TavilyClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_structured_results(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {
                "results": [
                    {
                        "title": "Q4 earnings release",
                        "url": "https://example.com/earnings",
                        "content": "Quarterly earnings summary",
                        "score": 0.93,
                    }
                ]
            }

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        response = await client.search("latest msft earnings", max_results=3)

        self.assertEqual(response.query, "latest msft earnings")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Q4 earnings release")
        self.assertEqual(captured[0][0], TAVILY_SEARCH_URL)
        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["api_key"], "tvly-test-key")
        self.assertEqual(payload["max_results"], 3)

    async def test_missing_api_key_fails_fast(self) -> None:
        called = False

        def fake_post(url: str, payload: object) -> object:
            nonlocal called
            called = True
            return {}

        client = TavilyClient(None, post_json=fake_post)
        with self.assertRaises(TavilyAPIKeyMissingError):
            await client.search("test")
        self.assertFalse(called)

    def test_post_json_sync_wraps_url_errors(self) -> None:
        client = TavilyClient("tvly-test-key")
        with patch("nycti.tavily.client.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(TavilyHTTPError):
                client._post_json_sync("https://example.com", {"query": "test"})


if __name__ == "__main__":
    unittest.main()
