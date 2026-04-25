import unittest
import sys
import types
import asyncio

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
    _extract_inline_tool_calls,
    _should_fail_over_chat_model,
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

    def test_extracts_xml_style_inline_tool_call_markup(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "checking\n"
                "<function_calls>\n"
                '<invoke name="stock_quote">\n'
                '<parameter name="symbol">NVDA</parameter>\n'
                "</invoke>\n"
                "</function_calls>"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "stock_quote",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        )

        self.assertEqual(text, "checking")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_xml_1")
        self.assertEqual(calls[0].name, "stock_quote")
        self.assertEqual(calls[0].arguments, '{"symbol":"NVDA"}')


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

    def test_detects_clarifai_nodepool_restriction_as_failover_signal(self) -> None:
        exc = Exception("Model 'Kimi-K2_6' is restricted to shared compute only. This request was routed to dedicated nodepool.")
        self.assertTrue(_should_fail_over_chat_model(exc))

    def test_fails_over_to_backup_chat_model_and_caches_primary_as_unhealthy(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.sambanova.ai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=("backup-model",),
        )
        client = OpenAIClient(settings)
        calls: list[str] = []

        async def fake_create(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "primary-model":
                raise Exception("Invalid model argument")
            message = types.SimpleNamespace(content="ok", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message)
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        first = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
            )
        )
        second = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello again"}],
                max_tokens=50,
                temperature=0.7,
            )
        )
        self.assertEqual(first.usage.model, "backup-model")
        self.assertEqual(second.usage.model, "backup-model")
        self.assertEqual(calls, ["primary-model", "backup-model", "backup-model"])


class EmbeddingTests(unittest.TestCase):
    def test_uses_dedicated_embedding_client_when_embedding_api_key_is_configured(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="chat-key",
            openai_embedding_api_key="embed-key",
            openai_embedding_base_url=None,
            openai_base_url="https://api.sambanova.ai/v1",
        )
        client = OpenAIClient(settings)
        self.assertEqual(client.client.kwargs, {"api_key": "chat-key", "base_url": "https://api.sambanova.ai/v1"})
        self.assertEqual(client.embedding_client.kwargs, {"api_key": "embed-key"})

        async def fail_if_used(**kwargs):
            raise AssertionError(f"chat client embeddings should not be used: {kwargs}")

        async def fake_embedding_create(**kwargs):
            usage = types.SimpleNamespace(prompt_tokens=12, total_tokens=12)
            data = [types.SimpleNamespace(embedding=[0.25, -0.5, 0.75])]
            return types.SimpleNamespace(data=data, usage=usage)

        client.client.embeddings.create = fail_if_used
        client.embedding_client.embeddings.create = fake_embedding_create
        result = asyncio.run(
            client.create_embedding(
                model="text-embedding-3-large",
                feature="memory_retrieve_embed",
                text="future of AI",
            )
        )
        self.assertEqual(result.embedding, [0.25, -0.5, 0.75])

    def test_embedding_client_reuses_main_provider_when_no_separate_embedding_key_is_configured(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="chat-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.sambanova.ai/v1",
        )
        client = OpenAIClient(settings)
        self.assertEqual(
            client.embedding_client.kwargs,
            {"api_key": "chat-key", "base_url": "https://api.sambanova.ai/v1"},
        )

    def test_embedding_client_uses_separate_embedding_base_url_when_configured(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="chat-key",
            openai_embedding_api_key="embed-key",
            openai_embedding_base_url="https://api.openai.com/v1",
            openai_base_url="https://api.sambanova.ai/v1",
        )
        client = OpenAIClient(settings)
        self.assertEqual(
            client.embedding_client.kwargs,
            {"api_key": "embed-key", "base_url": "https://api.openai.com/v1"},
        )

    def test_embedding_client_can_use_separate_base_url_with_inherited_api_key(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="chat-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url="https://api.openai.com/v1",
            openai_base_url="https://api.sambanova.ai/v1",
        )
        client = OpenAIClient(settings)
        self.assertEqual(
            client.embedding_client.kwargs,
            {"api_key": "chat-key", "base_url": "https://api.openai.com/v1"},
        )

    def test_rejects_blank_embedding_text(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="chat-key",
            openai_embedding_api_key="embed-key",
            openai_embedding_base_url=None,
            openai_base_url="https://api.sambanova.ai/v1",
        )
        client = OpenAIClient(settings)

        async def fail_if_used(**kwargs):
            raise AssertionError(f"embedding client should not be called: {kwargs}")

        client.embedding_client.embeddings.create = fail_if_used
        with self.assertRaises(ValueError):
            asyncio.run(
                client.create_embedding(
                    model="text-embedding-3-large",
                    feature="memory_retrieve_embed",
                    text="   \n\t  ",
                )
            )

    def test_detects_retryable_chat_model_failure(self) -> None:
        self.assertTrue(_should_fail_over_chat_model(Exception("Invalid model argument")))

    def test_detects_retryable_provider_connection_error(self) -> None:
        self.assertTrue(
            _should_fail_over_chat_model(
                Exception("Error code: 400 - {'description': 'Model prediction failed', 'developer_notes': 'Connection error.'}")
            )
        )


if __name__ == "__main__":
    unittest.main()
