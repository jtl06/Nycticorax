import unittest
import sys
import types
import asyncio
from unittest.mock import patch

fake_openai = types.ModuleType("openai")


class AsyncOpenAI:  # pragma: no cover - import shim for unit tests
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.embeddings = types.SimpleNamespace(create=None)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=None))


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from nycti.llm.client import (
    OpenAIClient,
    _build_chat_completion_request,
    _build_chat_completion_request_variants,
    _clarifai_embedding_model_candidates,
    _extract_inline_tool_calls,
    _is_clarifai_embedding_retryable_error,
    _is_token_field_conflict_error,
)


class InlineToolCallParsingTests(unittest.TestCase):
    def test_extracts_provider_inline_tool_call_markup(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|> call_1 <|tool_call_argument_begin|> "
                '{"query": "Micron expense guidance Q2 2026 earnings call operating expenses"} '
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        )
        self.assertEqual(text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")
        self.assertEqual(calls[0].name, "web_search")
        self.assertIn("Micron expense guidance", calls[0].arguments)

    def test_prefers_explicit_inline_tool_name_when_present(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "before\n"
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|> call_2 web_search <|tool_call_argument_begin|> "
                '{"query": "latest NVDA earnings call transcript"} '
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>\n"
                "after"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        )
        self.assertEqual(text, "before\n\nafter")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "web_search")


class ChatCompletionRequestTests(unittest.TestCase):
    def test_uses_max_tokens_for_text_only_messages(self) -> None:
        request = _build_chat_completion_request(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=300,
            temperature=0.7,
        )
        self.assertEqual(request["max_tokens"], 300)
        self.assertNotIn("max_completion_tokens", request)

    def test_uses_max_completion_tokens_for_image_messages(self) -> None:
        request = _build_chat_completion_request(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/chart.png"}},
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.7,
        )
        self.assertEqual(request["max_completion_tokens"], 300)
        self.assertNotIn("max_tokens", request)

    def test_image_requests_have_retry_variants(self) -> None:
        variants = _build_chat_completion_request_variants(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/chart.png"}},
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.7,
        )
        self.assertEqual(variants[0]["max_completion_tokens"], 300)
        self.assertEqual(variants[1]["max_tokens"], 300)
        self.assertNotIn("max_tokens", variants[0])
        self.assertNotIn("max_completion_tokens", variants[1])
        self.assertNotIn("max_tokens", variants[2])
        self.assertNotIn("max_completion_tokens", variants[2])

    def test_detects_token_field_conflict_error(self) -> None:
        exc = Exception("max_tokens and max_completion_tokens cannot both be set")
        self.assertTrue(_is_token_field_conflict_error(exc))


class EmbeddingTests(unittest.TestCase):
    def test_builds_clarifai_embedding_model_candidates(self) -> None:
        self.assertEqual(
            _clarifai_embedding_model_candidates(
                "https://clarifai.com/openai/embed/models/text-embedding-3-large"
            ),
            [
                "https://clarifai.com/openai/embed/models/text-embedding-3-large",
                "openai/embed/models/text-embedding-3-large",
                "text-embedding-3-large",
            ],
        )

    def test_detects_retryable_clarifai_embedding_error(self) -> None:
        detail = "Invalid model argument: Streaming is only supported for new type of models."
        self.assertTrue(_is_clarifai_embedding_retryable_error(detail))

    def test_uses_openai_compatible_embedding_request_for_clarifai_embed_urls(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-pat",
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
        )
        client = OpenAIClient(settings)
        with patch("nycti.llm.client._post_openai_compatible_embedding_request") as post_request:
            post_request.return_value = {
                "data": [{"embedding": [0.25, -0.5, 0.75]}],
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            }
            result = asyncio.run(
                client.create_embedding(
                    model="https://clarifai.com/openai/embed/models/text-embedding-3-large",
                    feature="memory_retrieve_embed",
                    text="future of AI",
                )
            )
        self.assertEqual(result.embedding, [0.25, -0.5, 0.75])
        post_request.assert_called_once_with(
            "https://api.clarifai.com/v2/ext/openai/v1",
            "test-pat",
            [
                "https://clarifai.com/openai/embed/models/text-embedding-3-large",
                "openai/embed/models/text-embedding-3-large",
                "text-embedding-3-large",
            ],
            "future of AI",
        )


if __name__ == "__main__":
    unittest.main()
