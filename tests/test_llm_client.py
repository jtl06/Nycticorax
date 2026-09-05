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
        self.responses = types.SimpleNamespace(create=None)


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)


class _FakeTransport:
    def __init__(self, *, chat_create=None, response_create=None):
        self.chat_create = chat_create
        self.response_create = response_create

    async def create_chat_completion(
        self, *, client, request, timeout_seconds, max_retries
    ):
        if self.chat_create is None:
            raise AssertionError("Unexpected chat-completions request")
        return await self.chat_create(**request)

    async def create_response(
        self, *, client, request, timeout_seconds, max_retries
    ):
        if self.response_create is None:
            raise AssertionError("Unexpected Responses API request")
        return await self.response_create(**request)


from nycti.llm.client import (
    OpenAIClient,
    _build_chat_completion_request,
    _is_deterministic_model_unavailable_error,
    _should_fail_over_chat_model,
    _should_retry_busy_foreground_chat,
    _summarize_provider_error,
    is_transient_provider_error,
)
from nycti.llm.provider_policy import (
    ProviderCapabilities,
    ProviderErrorKind,
    capabilities_for_base_url,
    classify_provider_error,
)


class ChatCompletionRequestTests(unittest.TestCase):
    def test_request_uses_one_explicit_token_field_without_image_probing(self) -> None:
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]}]
        for endpoint, model, field in (
            (None, "gpt-5.6-terra", "max_completion_tokens"),
            (None, "gpt-4.1-mini", "max_tokens"),
            ("https://api.deepinfra.com/v1/openai", "deepseek-ai/DeepSeek-V4-Pro-0813", "max_tokens"),
        ):
            with self.subTest(model=model):
                request = _build_chat_completion_request(
                    model=model, messages=messages, max_tokens=700, temperature=0.4,
                    capabilities=capabilities_for_base_url(endpoint),
                )
                self.assertEqual(700, request[field])
                self.assertIs(messages, request["messages"])
                self.assertEqual(1, len({"max_tokens", "max_completion_tokens"}.intersection(request)))

    def test_schema_failure_never_retries_with_stripped_context_or_tools(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test", openai_base_url="https://api.deepinfra.com/v1/openai",
            openai_embedding_api_key=None, openai_embedding_base_url=None,
            openai_chat_model="deepseek", openai_chat_model_fallbacks=(),
            openai_memory_model="memory",
        )
        calls = []
        async def fail(**request):
            calls.append(request)
            raise RuntimeError("invalid tool schema")
        client = OpenAIClient(settings, client_factory=AsyncOpenAI, transport=_FakeTransport(chat_create=fail))
        messages = [{"role": "system", "content": "Keep the supplied context."}, {"role": "user", "content": "Find the price."}]
        tools = [{"type": "function", "function": {"name": "quote", "parameters": {}}}]
        with self.assertRaisesRegex(RuntimeError, "invalid tool schema"):
            asyncio.run(client.complete_chat_turn(
                model="deepseek", feature="chat_reply", messages=messages,
                max_tokens=700, temperature=0.4, tools=tools,
            ))
        self.assertEqual(1, len(calls))
        self.assertEqual(messages, calls[0]["messages"])
        self.assertEqual(tools, calls[0]["tools"])

    def test_complete_chat_turn_can_disable_retries_and_set_timeout(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        options: list[dict[str, object]] = []
        calls: list[dict[str, object]] = []

        class FakeConfiguredClient:
            def __init__(self) -> None:
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            async def create(self, **kwargs):
                calls.append(kwargs)
                message = types.SimpleNamespace(content="ok", tool_calls=[], reasoning_content="")
                choice = types.SimpleNamespace(message=message, finish_reason="stop")
                usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
                return types.SimpleNamespace(choices=[choice], usage=usage)

        def fake_with_options(**kwargs):
            options.append(kwargs)
            return FakeConfiguredClient()

        client.client.with_options = fake_with_options

        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="optional_summary",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.2,
                request_timeout_seconds=8.0,
                request_max_retries=0,
            )
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(options, [{"timeout": 8.0, "max_retries": 0}])
        self.assertEqual(calls[0]["model"], "primary-model")
        self.assertEqual(calls[0]["max_tokens"], 50)

    def test_detects_clarifai_nodepool_restriction_as_failover_signal(self) -> None:
        exc = Exception("Model 'Kimi-K2_6' is restricted to shared compute only. This request was routed to dedicated nodepool.")
        self.assertTrue(_should_fail_over_chat_model(exc))

    def test_detects_missing_dedicated_deployment_as_deterministic(self) -> None:
        exc = Exception("Model prediction failed: requires a dedicated deployment but no deployed version was found.")
        self.assertTrue(_is_deterministic_model_unavailable_error(exc))

    def test_deduplicates_chat_model_candidates(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url=None,
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=("primary-model", "backup-model", "backup-model"),
            openai_memory_model="backup-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)

        self.assertEqual(
            ["primary-model", "backup-model"],
            client._chat_model_candidates("primary-model"),
        )

    def test_circuit_breaker_skips_repeated_missing_deployment_calls(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url=None,
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="missing-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls = 0

        async def fake_create(**_kwargs):
            nonlocal calls
            calls += 1
            raise Exception("Model prediction failed: requires a dedicated deployment.")

        client.transport = _FakeTransport(chat_create=fake_create)
        with self.assertRaisesRegex(Exception, "dedicated deployment"):
            asyncio.run(
                client.complete_chat_turn(
                    model="missing-model",
                    feature="memory_extract",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=50,
                    temperature=0.2,
                )
            )
        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            asyncio.run(
                client.complete_chat_turn(
                    model="missing-model",
                    feature="memory_extract",
                    messages=[{"role": "user", "content": "hello again"}],
                    max_tokens=50,
                    temperature=0.2,
                )
            )
        self.assertEqual(1, calls)
        self.assertFalse(client.is_model_available("missing-model"))
        self.assertTrue(client.is_model_available("primary-model"))

    def test_fails_over_to_backup_chat_model_and_caches_primary_as_unhealthy(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.sambanova.ai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=("backup-model",),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls: list[str] = []

        async def fake_create(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "primary-model":
                raise Exception("Invalid model argument")
            message = types.SimpleNamespace(content="ok", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message)
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.transport = _FakeTransport(chat_create=fake_create)
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

    def test_rate_limited_primary_fails_over_and_uses_short_circuit_breaker(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=("backup-model",),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls: list[str] = []

        async def fake_create(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "primary-model":
                raise Exception(
                    "Error code: 429 - Model is busy serving requests but took too long"
                )
            message = types.SimpleNamespace(content="backup answer", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.transport = _FakeTransport(chat_create=fake_create)
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
            )
        )

        self.assertEqual("backup answer", result.text)
        self.assertEqual(["primary-model", "primary-model", "backup-model"], calls)
        self.assertTrue(client._is_chat_model_unhealthy("primary-model"))

    def test_does_not_use_memory_model_as_implicit_chat_fallback(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="efficiency-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls: list[str] = []

        async def fake_create(**kwargs):
            calls.append(kwargs["model"])
            raise Exception("<html><head><title>403 Forbidden</title></head></html>")

        client.transport = _FakeTransport(chat_create=fake_create)
        with self.assertRaises(Exception):
            asyncio.run(
                client.complete_chat_turn(
                    model="primary-model",
                    feature="chat_reply",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=50,
                    temperature=0.7,
                )
            )

        self.assertEqual(calls, ["primary-model"])

class EmbeddingTests(unittest.TestCase):
    def test_uses_explicit_official_endpoint_when_base_urls_are_unset(self) -> None:
        settings = types.SimpleNamespace(openai_api_key="openai-key", openai_embedding_api_key=None, openai_embedding_base_url=None, openai_base_url=None)
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        expected = {"api_key": "openai-key", "base_url": "https://api.openai.com/v1"}
        self.assertEqual(expected, client.client.kwargs)
        self.assertEqual(expected, client.embedding_client.kwargs)

    def test_uses_dedicated_embedding_client_when_embedding_api_key_is_configured(self) -> None:
        settings = types.SimpleNamespace(openai_api_key="chat-key", openai_embedding_api_key="embed-key", openai_embedding_base_url=None, openai_base_url="https://api.sambanova.ai/v1")
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        self.assertEqual(client.client.kwargs, {"api_key": "chat-key", "base_url": "https://api.sambanova.ai/v1"})
        self.assertEqual(client.embedding_client.kwargs,
                         {"api_key": "embed-key", "base_url": "https://api.openai.com/v1"})
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
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
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
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
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
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
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
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)

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

    def test_detects_transient_provider_busy_error(self) -> None:
        self.assertTrue(
            is_transient_provider_error(
                Exception(
                    "Error code: 429 - {'description': 'Model is busy serving requests but took too long'}"
                )
            )
        )
        self.assertFalse(is_transient_provider_error(Exception("invalid tool schema")))

    def test_retries_busy_provider_only_for_foreground_chat(self) -> None:
        busy = Exception("429: Model is busy serving requests but took too long")
        self.assertTrue(_should_retry_busy_foreground_chat("chat_reply", busy))
        self.assertTrue(_should_retry_busy_foreground_chat("chat_reply_final", busy))
        self.assertFalse(_should_retry_busy_foreground_chat("memory_extract", busy))
        incompatible = Exception("invalid tool schema")
        self.assertFalse(_should_retry_busy_foreground_chat("chat_reply", incompatible))

    def test_busy_foreground_chat_retries_same_model_once(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls = 0
        async def fake_create(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Exception("429: Model is busy serving requests but took too long")
            message = types.SimpleNamespace(content="ok", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)
        client.transport = _FakeTransport(chat_create=fake_create)
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
            )
        )

        self.assertEqual("ok", result.text)
        self.assertEqual(2, calls)
        self.assertEqual(2, result.usage.attempt)

    def test_busy_memory_call_does_not_retry(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings, client_factory=AsyncOpenAI)
        calls = 0
        async def fake_create(**_kwargs):
            nonlocal calls
            calls += 1
            raise Exception("429: Model is busy serving requests but took too long")

        client.transport = _FakeTransport(chat_create=fake_create)
        with self.assertRaisesRegex(Exception, "Model is busy"):
            asyncio.run(
                client.complete_chat_turn(
                    model="memory-model",
                    feature="memory_extract",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=50,
                    temperature=0,
                )
            )

        self.assertEqual(1, calls)

    def test_detects_provider_html_403_as_failover_signal(self) -> None:
        self.assertTrue(
            _should_fail_over_chat_model(
                Exception(
                    "<html><head><title>403 Forbidden</title></head>"
                    "<body><center><h1>403 Forbidden</h1></center></body></html>"
                )
            )
        )

    def test_provider_policy_distinguishes_tool_auth_and_deployment_errors(self) -> None:
        self.assertEqual(
            ProviderErrorKind.TOOL_INCOMPATIBLE,
            classify_provider_error(Exception("Invalid tool schema")),
        )
        self.assertEqual(
            ProviderErrorKind.AUTHENTICATION,
            classify_provider_error(Exception("401 Unauthorized: invalid API key")),
        )
        self.assertEqual(
            ProviderErrorKind.DEPLOYMENT,
            classify_provider_error(Exception("No deployed version was found")),
        )

    def test_provider_error_summary_strips_html_and_truncates(self) -> None:
        summary = _summarize_provider_error(
            Exception(
                "<html><head><title>403 Forbidden</title></head>"
                "<body><center><h1>403 Forbidden</h1></center></body></html>"
            )
        )

        self.assertIn("Exception:", summary)
        self.assertIn("403 Forbidden", summary)
        self.assertNotIn("<html>", summary)


if __name__ == "__main__":
    unittest.main()
