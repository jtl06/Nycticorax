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
    _build_chat_completion_request_variants,
    _compact_plain_retry_messages,
    _extract_inline_tool_calls,
    _is_deterministic_model_unavailable_error,
    _strip_inline_tool_call_markup,
    _should_fail_over_chat_model,
    _should_retry_busy_foreground_chat,
    _should_retry_without_native_tools,
    _strip_tool_guidance_messages,
    _summarize_provider_error,
    _is_token_field_conflict_error,
    _plain_chat_retry_messages,
    is_transient_provider_error,
)
from nycti.llm.provider_policy import (
    ProviderCapabilities,
    ProviderErrorKind,
    capabilities_for_base_url,
    classify_provider_error,
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

    def test_infers_unnamed_inline_tool_from_unique_argument_shape(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|> call_1 <|tool_call_argument_begin|> "
                '{"queries":["NVIDIA earnings","AMD earnings"]}'
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "web",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "queries": {"type": "array"},
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "img_search",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                },
            ],
        )

        self.assertEqual("", text)
        self.assertEqual(1, len(calls))
        self.assertEqual("web", calls[0].name)

    def test_defaults_unnamed_public_url_to_url_extract(self) -> None:
        _text, calls = _extract_inline_tool_calls(
            (
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|> call_1 <|tool_call_argument_begin|>"
                '{"url":"https://investor.example.com/earnings"}'
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "url_extract",
                        "parameters": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}, "query": {"type": "string"}},
                            "required": ["url"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "browser_extract",
                        "parameters": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}, "headed": {"type": "boolean"}},
                            "required": ["url"],
                        },
                    },
                },
            ],
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("url_extract", calls[0].name)

    def test_extracts_functions_namespace_inline_tool_call_header(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "I'll pull up that tweet and see what's going on."
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|>functions.extract_url_content:0<|tool_call_argument_begin|>"
                '{"url":"https://fixupx.com/KanekoaTheGreat/status/2048236067132940547"}'
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>"
            ),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "extract_url_content",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        )

        self.assertEqual(text, "I'll pull up that tweet and see what's going on.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "extract_url_content")
        self.assertIn("fixupx.com", calls[0].arguments)

    def test_strips_unknown_inline_tool_call_markup(self) -> None:
        text, calls = _extract_inline_tool_calls(
            (
                "I'll check if there's been a recent assassination attempt."
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|>functions.web_search:0<|tool_call_argument_begin|>"
                '{"query":"Trump assassination attempt April 2026 market reaction Monday"}'
                "<|tool_call_end|>"
                "<|tool_calls_section_end|>"
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

        self.assertEqual(text, "I'll check if there's been a recent assassination attempt.")
        self.assertEqual(calls, [])
        self.assertNotIn("<|tool_calls_section_begin|>", text)

    def test_strip_inline_tool_call_markup_handles_partial_sections(self) -> None:
        text = _strip_inline_tool_call_markup(
            "checking\n<|tool_calls_section_begin|><|tool_call_begin|>functions.web_search:0"
        )

        self.assertEqual(text, "checking")

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

    def test_complete_chat_turn_strips_tool_markup_when_no_tools_are_exposed(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url=None,
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
        )
        client = OpenAIClient(settings)

        async def fake_create(**kwargs):
            message = types.SimpleNamespace(
                content=(
                    "I'll check that."
                    "<|tool_calls_section_begin|>"
                    "<|tool_call_begin|>functions.web_search:0<|tool_call_argument_begin|>"
                    '{"query":"latest news"}'
                    "<|tool_call_end|>"
                    "<|tool_calls_section_end|>"
                ),
                tool_calls=[],
                reasoning_content="",
            )
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create

        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
                tools=None,
            )
        )

        self.assertEqual(result.text, "I'll check that.")
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.finish_reason, "stop")
        self.assertIn("<|tool_calls_section_begin|>", result.raw_text)


class ChatCompletionRequestTests(unittest.TestCase):
    def test_uses_max_tokens_for_text_only_messages(self) -> None:
        request = _build_chat_completion_request_variants(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=300,
            temperature=0.7,
        )[0]
        self.assertEqual(request["max_tokens"], 300)
        self.assertNotIn("max_completion_tokens", request)

    def test_uses_max_completion_tokens_for_image_messages(self) -> None:
        request = _build_chat_completion_request_variants(
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
        )[0]
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

    def test_complete_chat_turn_can_disable_retries_and_set_timeout(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
        )
        client = OpenAIClient(settings)
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
        client = OpenAIClient(settings)

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
        client = OpenAIClient(settings)
        calls = 0

        async def fake_create(**_kwargs):
            nonlocal calls
            calls += 1
            raise Exception("Model prediction failed: requires a dedicated deployment.")

        client.client.chat.completions.create = fake_create
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
        client = OpenAIClient(settings)
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

        client.client.chat.completions.create = fake_create
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

    def test_uses_efficiency_model_as_last_resort_chat_fallback(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="efficiency-model",
        )
        client = OpenAIClient(settings)
        calls: list[str] = []

        async def fake_create(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "primary-model":
                raise Exception("<html><head><title>403 Forbidden</title></head></html>")
            message = types.SimpleNamespace(content="ok from fallback", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
            )
        )

        self.assertEqual(result.text, "ok from fallback")
        self.assertEqual(result.usage.model, "efficiency-model")
        self.assertEqual(calls, ["primary-model", "efficiency-model"])

    def test_retries_tool_request_without_native_tools_on_explicit_schema_error(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings)
        call_has_tools: list[bool] = []
        message_counts: list[int] = []

        async def fake_create(**kwargs):
            call_has_tools.append("tools" in kwargs)
            message_counts.append(len(kwargs["messages"]))
            if "tools" in kwargs:
                raise Exception("Invalid tool schema: tools are not supported for this deployment.")
            message = types.SimpleNamespace(content="ok without native tools", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "hello"},
                    {"role": "user", "content": "Available tools this turn:\n- web_search"},
                    {"role": "user", "content": "Tool-loop discipline: answer after tools."},
                ],
                max_tokens=50,
                temperature=0.7,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
            )
        )

        self.assertEqual(result.text, "ok without native tools")
        self.assertIn('"tools"', result.native_tool_failure_request_json)
        self.assertIn('"messages"', result.native_tool_failure_request_json)
        self.assertEqual(call_has_tools, [True, False])
        self.assertEqual(message_counts, [4, 2])

    def test_can_parse_inline_tools_without_sending_native_tool_schema(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings)
        sent_tools: list[bool] = []

        async def fake_create(**kwargs):
            sent_tools.append("tools" in kwargs)
            message = types.SimpleNamespace(
                content=(
                    "<function_calls>\n"
                    '<invoke name="web_search">\n'
                    '<parameter name="query">nycti discord bot</parameter>\n'
                    "</invoke>\n"
                    "</function_calls>"
                ),
                tool_calls=[],
                reasoning_content="",
            )
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "use search"}],
                max_tokens=50,
                temperature=0.7,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
                use_native_tools=False,
            )
        )

        self.assertEqual(sent_tools, [False])
        self.assertEqual(result.text, "")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "web_search")
        self.assertIn("nycti discord bot", result.tool_calls[0].arguments)

    def test_retries_compact_plain_chat_when_stripped_retry_is_forbidden(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings)
        message_counts: list[int] = []
        prompts: list[list[dict[str, object]]] = []

        async def fake_create(**kwargs):
            prompts.append(kwargs["messages"])
            message_counts.append(len(kwargs["messages"]))
            if "tools" in kwargs:
                raise Exception("Invalid tool schema: tools are not supported for this deployment.")
            if len(kwargs["messages"]) > 2 or "Current user:" in kwargs["messages"][1]["content"]:
                raise Exception("<html><head><title>403 Forbidden</title></head></html>")
            message = types.SimpleNamespace(content="ok compact", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[
                    {"role": "system", "content": "system"},
                    {
                        "role": "user",
                        "content": (
                            "Current user: mat\n\n"
                            "Current local date/time:\nSunday, May 17, 2026 17:00 PDT\n\n"
                            "Current request:\nwhat happened?\n\n"
                            "Recent channel context:\nmat: hello\n\n"
                            "Extended channel context:\n(none)"
                        ),
                    },
                    {"role": "user", "content": "Available tools this turn:\n- web_search"},
                    {"role": "user", "content": "Tool-loop discipline: answer after tools."},
                ],
                max_tokens=50,
                temperature=0.7,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
            )
        )

        self.assertEqual(result.text, "ok compact")
        self.assertEqual(message_counts, [4, 2, 2])
        self.assertIn("Current request:\nwhat happened?", prompts[2][1]["content"])
        self.assertIn("Recent context:\nmat: hello", prompts[2][1]["content"])
        self.assertNotIn("Available tools this turn:", prompts[2][1]["content"])
        self.assertNotIn("Tool-loop discipline:", prompts[2][1]["content"])

    def test_strip_tool_guidance_messages_removes_appended_tool_instructions(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "Available tools this turn:\n- web_search"},
            {"role": "user", "content": "Tool-loop discipline: answer after tools."},
        ]

        stripped = _strip_tool_guidance_messages(messages)

        self.assertEqual(stripped, messages[:2])

    def test_plain_chat_retry_messages_convert_tool_protocol_messages(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "web_search",
                "content": "Search result text",
            },
        ]

        plain = _plain_chat_retry_messages(messages)

        self.assertEqual(len(plain), 2)
        self.assertNotIn("tool_calls", plain[0])
        self.assertEqual(plain[1]["role"], "user")
        self.assertEqual(plain[1]["content"], "Tool result from web_search:\nSearch result text")

    def test_compact_plain_retry_messages_extracts_request_and_recent_context(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": (
                    "Current user: mat\n\n"
                    "Current local date/time:\nSunday, May 17, 2026 17:00 PDT\n\n"
                    "Current request:\nwhat happened?\n\n"
                    "Recent channel context:\nmat: hello\n\n"
                    "Extended channel context:\n(none)"
                ),
            },
        ]

        compact = _compact_plain_retry_messages(messages)

        self.assertEqual(len(compact), 2)
        self.assertIn("what happened?", compact[1]["content"])
        self.assertIn("mat: hello", compact[1]["content"])
        self.assertNotIn("Current user: mat", compact[1]["content"])


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
        client = OpenAIClient(settings)
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
        client.client.chat.completions.create = fake_create
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
        client = OpenAIClient(settings)
        calls = 0
        async def fake_create(**_kwargs):
            nonlocal calls
            calls += 1
            raise Exception("429: Model is busy serving requests but took too long")

        client.client.chat.completions.create = fake_create
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

    def test_does_not_misclassify_opaque_403_as_tool_incompatibility(self) -> None:
        self.assertFalse(
            _should_retry_without_native_tools(
                Exception("<html><head><title>403 Forbidden</title></head></html>")
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

    def test_clarifai_capabilities_define_explicit_request_policy(self) -> None:
        capabilities = capabilities_for_base_url(
            "https://api.clarifai.com/v2/ext/openai/v1"
        )

        self.assertEqual("clarifai", capabilities.name)
        self.assertTrue(capabilities.native_tools)
        self.assertEqual(("max_tokens",), capabilities.text_token_fields)
        self.assertEqual(0, capabilities.request_max_retries)

    def test_kimi_efficiency_calls_disable_thinking(self) -> None:
        kimi_model = "https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5"
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model=kimi_model,
            openai_chat_model_fallbacks=(),
            openai_memory_model=kimi_model,
        )
        client = OpenAIClient(settings)
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs):
            calls.append(kwargs)
            message = types.SimpleNamespace(content='{"should_store": false}', tool_calls=[])
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        asyncio.run(
            client.complete_chat_turn(
                model=kimi_model,
                feature="memory_extract",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0,
            )
        )

        self.assertEqual(
            {"chat_template_kwargs": {"thinking": False}},
            calls[0]["extra_body"],
        )

    def test_kimi_main_chat_keeps_default_thinking_mode(self) -> None:
        kimi_model = "https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5"
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model=kimi_model,
            openai_chat_model_fallbacks=(),
            openai_memory_model=kimi_model,
        )
        client = OpenAIClient(settings)
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs):
            calls.append(kwargs)
            message = types.SimpleNamespace(content="hello", tool_calls=[])
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        asyncio.run(
            client.complete_chat_turn(
                model=kimi_model,
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
            )
        )

        self.assertNotIn("extra_body", calls[0])

    def test_provider_capability_can_disable_native_schema_submission(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="test-key",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_base_url="https://compatible.example/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="memory-model",
        )
        client = OpenAIClient(settings)
        client.provider_capabilities = ProviderCapabilities(
            name="plain-provider",
            label="plain-provider",
            native_tools=False,
            vision=False,
            text_token_fields=("max_tokens",),
            image_token_fields=("max_tokens",),
            request_timeout_seconds=5,
            request_max_retries=0,
        )
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs):
            calls.append(kwargs)
            message = types.SimpleNamespace(content="plain answer", tool_calls=[], reasoning_content="")
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fake_create
        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
                tools=[{"type": "function", "function": {"name": "web", "parameters": {}}}],
            )
        )

        self.assertNotIn("tools", calls[0])
        self.assertTrue(result.native_tool_calling_failed)

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
