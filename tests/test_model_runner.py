import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.model_runner import call_agent_model
from nycti.chat.run_state import AgentRun
from nycti.llm.client import OpenAIClient


class ModelRunnerDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_primary_leaves_time_for_fallback(self):
        settings = SimpleNamespace(
            openai_api_key="unused", openai_base_url=None,
            openai_chat_model="gpt-5.6-primary", openai_chat_model_fallbacks=(),
            openai_memory_model="gpt-5.6-primary",
            openai_embedding_api_key=None, openai_embedding_base_url=None,
            openai_fallback_api_key="unused", openai_fallback_base_url="https://fallback.invalid/v1",
            openai_fallback_chat_model="fallback-model",
        )
        client = OpenAIClient(settings)
        client.provider_capabilities = replace(client.provider_capabilities, request_timeout_seconds=0.02)
        cancelled = asyncio.Event()

        async def slow_primary(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def fallback(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="A grounded answer.", tool_calls=[], reasoning_content=""),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )

        client.client = SimpleNamespace(responses=SimpleNamespace(create=slow_primary))
        client.fallback_client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fallback)))
        run = AgentRun(messages=[{"role": "user", "content": "market report"}])
        metrics = {}
        turn = await call_agent_model(
            llm_client=client, run=run, chat_model="gpt-5.6-primary", feature="chat_reply",
            max_tokens=100, temperature=0.4, tools=None, timeout_seconds=0.5,
            metrics=metrics, trace=AgentTrace(enabled=True),
        )
        self.assertEqual("A grounded answer.", turn.text)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(["error", "ok"], [a.status for a in turn.provider_attempts])
        self.assertGreaterEqual(turn.provider_attempts[0].request_ms, 10)
        self.assertEqual(2, metrics["provider_attempt_count"])
        self.assertEqual(1, run.model_turns)

    async def test_user_cancellation_records_time_without_recovery(self):
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        metrics = {}
        run = AgentRun(messages=[])
        task = asyncio.create_task(call_agent_model(
            llm_client=SimpleNamespace(complete_chat_turn=pending), run=run,
            chat_model="test", feature="chat_reply", max_tokens=100, temperature=0.4,
            tools=None, timeout_seconds=10, metrics=metrics, trace=AgentTrace(enabled=True),
        ))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("cancelled", run.step_records[-1].status)
        self.assertEqual(1, metrics["chat_cancelled_turn_count"])
        self.assertIn("chat_llm_ms", metrics)
        self.assertNotIn("chat_provider_failure_count", metrics)
        self.assertEqual(0, run.model_turns)
