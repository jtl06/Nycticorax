import unittest
from unittest.mock import patch
from urllib.error import URLError

from nycti.tavily.client import TAVILY_EXTRACT_URL, TAVILY_SEARCH_URL, TavilyClient
from nycti.tavily.formatting import (
    format_tavily_extract_message,
    format_tavily_image_search_message,
    format_tavily_search_message,
)
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
                    published_date="2026-07-09",
                )
            ],
        )
        message = format_tavily_search_message(response)
        self.assertIn("Tavily web results for: apple earnings", message)
        self.assertIn("Apple Investor Relations", message)
        self.assertIn("https://investor.apple.com", message)
        self.assertIn("Published: 2026-07-09", message)

    def test_format_extract_message_includes_url_title_and_content(self) -> None:
        from nycti.tavily.models import TavilyExtractResponse, TavilyExtractResult

        response = TavilyExtractResponse(
            url="https://example.com/post",
            query="expense guidance",
            results=[
                TavilyExtractResult(
                    url="https://example.com/post",
                    title="Investor update",
                    raw_content="Micron guided operating expenses higher next quarter.",
                )
            ],
        )
        message = format_tavily_extract_message(response)
        self.assertIn("Tavily extract for: https://example.com/post", message)
        self.assertIn("Title: Investor update", message)
        self.assertIn("Focus: expense guidance", message)
        self.assertIn("Micron guided operating expenses", message)

    def test_format_image_search_message_includes_direct_image_urls(self) -> None:
        from nycti.tavily.models import TavilySearchResponse

        response = TavilySearchResponse(
            query="cartier tank watch",
            results=[],
            images=[
                "https://example.com/cartier-tank-1.jpg",
                "https://example.com/cartier-tank-2.jpg",
            ],
        )
        message = format_tavily_image_search_message(response)
        self.assertIn("Tavily image results for: cartier tank watch", message)
        self.assertIn("https://example.com/cartier-tank-1.jpg", message)


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
                        "published_date": "2026-07-09",
                    }
                ]
            }

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        response = await client.search("latest msft earnings", max_results=3)

        self.assertEqual(response.query, "latest msft earnings")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Q4 earnings release")
        self.assertEqual(response.results[0].published_date, "2026-07-09")
        self.assertEqual(captured[0][0], TAVILY_SEARCH_URL)
        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["api_key"], "tvly-test-key")
        self.assertEqual(payload["search_depth"], "ultra-fast")
        self.assertEqual(payload["max_results"], 3)
        self.assertEqual(payload["include_images"], False)

    async def test_search_depth_can_be_configured(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {"results": []}

        client = TavilyClient("tvly-test-key", search_depth="fast", post_json=fake_post)
        await client.search("latest msft earnings")

        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["search_depth"], "fast")

    async def test_search_depth_can_be_overridden_per_call(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {"results": []}

        client = TavilyClient("tvly-test-key", search_depth="ultra-fast", post_json=fake_post)
        await client.search("latest msft earnings", search_depth="basic")

        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["search_depth"], "basic")

    async def test_search_accepts_finance_topic_and_time_range(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {"results": []}

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        await client.search(
            "SPCX ticker",
            search_depth="basic",
            topic="finance",
            time_range="week",
        )

        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["search_depth"], "basic")
        self.assertEqual(payload["topic"], "finance")
        self.assertEqual(payload["time_range"], "week")

    def test_invalid_search_depth_raises(self) -> None:
        with self.assertRaises(ValueError):
            TavilyClient("tvly-test-key", search_depth="turbo")

    async def test_image_search_returns_image_urls(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {
                "results": [],
                "images": [
                    "https://example.com/cartier-tank-1.jpg",
                    {"url": "https://example.com/cartier-tank-2.jpg"},
                ],
            }

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        response = await client.image_search("cartier tank watch", max_results=2)

        self.assertEqual(response.query, "cartier tank watch")
        self.assertEqual(
            response.images,
            ["https://example.com/cartier-tank-1.jpg", "https://example.com/cartier-tank-2.jpg"],
        )
        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["include_images"], True)

    async def test_extract_returns_structured_results(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {
                "results": [
                    {
                        "url": "https://example.com/article",
                        "title": "Example article",
                        "raw_content": "This page contains the extracted body content.",
                    }
                ]
            }

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        response = await client.extract("https://example.com/article", query="main points")

        self.assertEqual(response.url, "https://example.com/article")
        self.assertEqual(response.query, "main points")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Example article")
        self.assertEqual(captured[0][0], TAVILY_EXTRACT_URL)
        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["api_key"], "tvly-test-key")
        self.assertEqual(payload["urls"], ["https://example.com/article"])
        self.assertEqual(payload["query"], "main points")
        self.assertEqual(payload["chunks_per_source"], 3)

    async def test_extract_can_request_five_focused_chunks(self) -> None:
        captured: list[tuple[str, object]] = []

        def fake_post(url: str, payload: object) -> object:
            captured.append((url, payload))
            return {"results": []}

        client = TavilyClient("tvly-test-key", post_json=fake_post)
        await client.extract(
            "https://example.com/article",
            query="revenue guidance",
            chunks_per_source=5,
        )

        payload = captured[0][1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["chunks_per_source"], 5)

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
