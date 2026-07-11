import asyncio
import sys
import types
import unittest


fake_openai = types.ModuleType("openai")


class AsyncOpenAI:  # pragma: no cover - import shim
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.embeddings = types.SimpleNamespace(create=None)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=None))
        self.responses = types.SimpleNamespace(create=None)


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from nycti.llm.client import OpenAIClient


class CrossProviderFallbackTests(unittest.TestCase):
    def test_foreground_chat_fails_over_to_separate_provider(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="primary-key",
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="primary-model",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_fallback_api_key="fallback-key",
            openai_fallback_base_url="https://api.deepinfra.com/v1/openai",
            openai_fallback_chat_model="gpt-5-fallback",
        )
        client = OpenAIClient(settings)
        assert client.fallback_client is not None
        primary_calls = 0
        fallback_calls = 0

        async def fail_primary(**_kwargs):
            nonlocal primary_calls
            primary_calls += 1
            raise Exception("403 Forbidden")

        async def succeed_fallback(**kwargs):
            nonlocal fallback_calls
            fallback_calls += 1
            self.assertEqual("gpt-5-fallback", kwargs["model"])
            self.assertEqual("low", kwargs["reasoning_effort"])
            message = types.SimpleNamespace(
                content="fallback answer",
                tool_calls=[],
                reasoning_content="",
            )
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fail_primary
        client.fallback_client.client.chat.completions.create = succeed_fallback

        first = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
                reasoning_effort_override="low",
            )
        )
        second = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello again"}],
                max_tokens=50,
                temperature=0.7,
                reasoning_effort_override="low",
            )
        )

        self.assertEqual("fallback answer", first.text)
        self.assertEqual("api.deepinfra.com", first.usage.provider)
        self.assertEqual("primary-model", first.usage.requested_model)
        self.assertEqual(2, first.usage.attempt)
        self.assertEqual([1, 2], [attempt.attempt for attempt in first.provider_attempts])
        self.assertEqual(
            ["clarifai", "api.deepinfra.com"],
            [attempt.provider for attempt in first.provider_attempts],
        )
        self.assertEqual(["error", "ok"], [attempt.status for attempt in first.provider_attempts])
        self.assertEqual(1, primary_calls)
        self.assertEqual(2, fallback_calls)
        self.assertEqual("fallback answer", second.text)

    def test_responses_failure_forwards_reasoning_override_to_fallback(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="primary-key",
            openai_base_url=None,
            openai_chat_model="gpt-5.6-primary",
            openai_chat_model_fallbacks=(),
            openai_memory_model="gpt-5.6-primary",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_fallback_api_key="fallback-key",
            openai_fallback_base_url="https://api.deepinfra.com/v1/openai",
            openai_fallback_chat_model="gpt-5-fallback",
        )
        client = OpenAIClient(settings)
        assert client.fallback_client is not None

        async def fail_primary(**_kwargs):
            raise Exception("404 model not found")

        async def succeed_fallback(**kwargs):
            self.assertEqual("low", kwargs["reasoning_effort"])
            message = types.SimpleNamespace(
                content="fallback response",
                tool_calls=[],
                reasoning_content="",
            )
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.responses.create = fail_primary
        client.fallback_client.client.chat.completions.create = succeed_fallback

        result = asyncio.run(
            client.complete_chat_turn(
                model="gpt-5.6-primary",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=50,
                temperature=0.7,
                reasoning_effort_override="low",
            )
        )

        self.assertEqual("fallback response", result.text)
        self.assertEqual("api.deepinfra.com", result.usage.provider)

    def test_background_memory_fails_over_to_separate_provider(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="primary-key",
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="primary-model",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_fallback_api_key="fallback-key",
            openai_fallback_base_url="https://api.deepinfra.com/v1/openai",
            openai_fallback_chat_model="fallback-model",
        )
        client = OpenAIClient(settings)
        assert client.fallback_client is not None
        fallback_calls = 0

        async def fail_primary(**_kwargs):
            raise Exception("403 Forbidden")

        async def succeed_fallback(**_kwargs):
            nonlocal fallback_calls
            fallback_calls += 1
            message = types.SimpleNamespace(
                content='{"should_store": false}',
                tool_calls=[],
                reasoning_content="",
            )
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            return types.SimpleNamespace(choices=[choice], usage=usage)

        client.client.chat.completions.create = fail_primary
        client.fallback_client.client.chat.completions.create = succeed_fallback

        result = asyncio.run(
            client.complete_chat_turn(
                model="primary-model",
                feature="memory_extract",
                messages=[{"role": "user", "content": "remember this"}],
                max_tokens=50,
                temperature=0,
            )
        )
        self.assertEqual('{"should_store": false}', result.text)
        self.assertEqual("api.deepinfra.com", result.usage.provider)
        self.assertEqual(1, fallback_calls)

    def test_failed_cross_provider_chain_is_attached_to_exception(self) -> None:
        settings = types.SimpleNamespace(
            openai_api_key="primary-key",
            openai_base_url="https://api.clarifai.com/v2/ext/openai/v1",
            openai_chat_model="primary-model",
            openai_chat_model_fallbacks=(),
            openai_memory_model="primary-model",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_fallback_api_key="fallback-key",
            openai_fallback_base_url="https://api.deepinfra.com/v1/openai",
            openai_fallback_chat_model="fallback-model",
        )
        client = OpenAIClient(settings)
        assert client.fallback_client is not None

        async def fail(**_kwargs):
            raise Exception("403 Forbidden")

        client.client.chat.completions.create = fail
        client.fallback_client.client.chat.completions.create = fail

        with self.assertRaises(Exception) as raised:
            asyncio.run(
                client.complete_chat_turn(
                    model="primary-model",
                    feature="chat_reply",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=50,
                    temperature=0.7,
                )
            )

        attempts = raised.exception.nycti_provider_attempts
        self.assertEqual([1, 2], [attempt.attempt for attempt in attempts])
        self.assertEqual(
            ["clarifai", "api.deepinfra.com"],
            [attempt.provider for attempt in attempts],
        )
        self.assertEqual(["error", "error"], [attempt.status for attempt in attempts])


if __name__ == "__main__":
    unittest.main()
