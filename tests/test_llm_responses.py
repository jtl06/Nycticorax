import asyncio
from types import SimpleNamespace
import unittest

from nycti.chat.loop_messages import append_assistant_tool_call_message
from nycti.llm.client import OpenAIClient
from nycti.llm.responses_adapter import build_responses_request, parse_responses_turn


class ResponsesAdapterTests(unittest.TestCase):
    def test_converts_chat_history_tools_and_images(self) -> None:
        request = build_responses_request(
            model="gpt-5.6-luna",
            messages=[
                {"role": "system", "content": "Be concise."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read this."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example/chart.png"},
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web", "arguments": '{"query":"latest"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "web",
                    "content": "fresh result",
                },
            ],
            max_tokens=700,
            temperature=0.7,
            reasoning_effort="high",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "web",
                        "description": "Search",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

        self.assertEqual(request["instructions"], "Be concise.")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        self.assertEqual(request["max_output_tokens"], 700)
        self.assertFalse(request["store"])
        self.assertNotIn("temperature", request)
        self.assertEqual(request["tools"][0]["name"], "web")
        self.assertEqual(request["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(request["input"][1]["type"], "function_call")
        self.assertEqual(request["input"][2]["type"], "function_call_output")

    def test_client_routes_gpt_56_through_responses_and_parses_tool_call(self) -> None:
        settings = SimpleNamespace(
            openai_api_key="test-key",
            openai_base_url=None,
            openai_chat_model="gpt-5.6-luna",
            openai_chat_model_fallbacks=(),
            openai_memory_model="gpt-5.6-luna",
            openai_vision_model="gpt-5.6-luna",
            openai_reasoning_effort="high",
            openai_efficiency_reasoning_effort="minimal",
            openai_embedding_api_key=None,
            openai_embedding_base_url=None,
            openai_fallback_api_key=None,
            openai_fallback_base_url=None,
            openai_fallback_chat_model=None,
        )
        client = OpenAIClient(settings)
        calls: list[dict[str, object]] = []

        async def fake_create(request_kwargs, **_options):
            calls.append(request_kwargs)
            return SimpleNamespace(
                model="gpt-5.6-luna",
                status="completed",
                error=None,
                output_text="",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        call_id="call_1",
                        name="web",
                        arguments='{"query":"latest"}',
                    )
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
            )

        client._create_response = fake_create
        turn = asyncio.run(
            client.complete_chat_turn(
                model="gpt-5.6-luna",
                feature="chat_reply",
                messages=[{"role": "user", "content": "what happened today?"}],
                max_tokens=700,
                temperature=0.7,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "web",
                            "description": "Search",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        )

        self.assertEqual(calls[0]["reasoning"], {"effort": "high"})
        self.assertEqual(turn.finish_reason, "tool_calls")
        self.assertEqual(turn.tool_calls[0].name, "web")
        self.assertEqual(turn.usage.total_tokens, 15)

    def test_stateless_tool_turn_replays_encrypted_reasoning_before_function_output(self) -> None:
        client = OpenAIClient(_settings())
        calls: list[dict[str, object]] = []

        async def fake_create(request_kwargs, **_options):
            calls.append(request_kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    model="gpt-5.6-luna",
                    status="completed",
                    error=None,
                    output_text="",
                    output=[
                        SimpleNamespace(
                            id="rs_1",
                            type="reasoning",
                            summary=[SimpleNamespace(type="summary_text", text="I need fresh data.")],
                            encrypted_content="encrypted-reasoning-state",
                        ),
                        SimpleNamespace(
                            id="fc_1",
                            type="function_call",
                            call_id="call_1",
                            name="web",
                            arguments='{"query":"latest"}',
                            status="completed",
                        ),
                    ],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                )
            return SimpleNamespace(
                model="gpt-5.6-luna",
                status="completed",
                error=None,
                output_text="Grounded answer.",
                output=[],
                usage=SimpleNamespace(input_tokens=25, output_tokens=4, total_tokens=29),
            )

        client._create_response = fake_create
        messages: list[dict[str, object]] = [{"role": "user", "content": "What happened?"}]
        first_turn = asyncio.run(
            client.complete_chat_turn(
                model="gpt-5.6-luna",
                feature="chat_reply",
                messages=messages,
                max_tokens=700,
                temperature=0.7,
                tools=[_web_tool()],
            )
        )
        append_assistant_tool_call_message(messages, first_turn)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "web",
                "content": "fresh result",
            }
        )
        second_turn = asyncio.run(
            client.complete_chat_turn(
                model="gpt-5.6-luna",
                feature="chat_reply",
                messages=messages,
                max_tokens=700,
                temperature=0.4,
                tools=[_web_tool()],
            )
        )

        second_input = calls[1]["input"]
        self.assertEqual(second_input[1]["type"], "reasoning")
        self.assertEqual(second_input[1]["encrypted_content"], "encrypted-reasoning-state")
        self.assertEqual(second_input[2]["type"], "function_call")
        self.assertEqual(second_input[3]["type"], "function_call_output")
        self.assertEqual(second_input[3]["call_id"], "call_1")
        self.assertEqual(second_turn.text, "Grounded answer.")

    def test_parses_refusal_incomplete_details_reasoning_and_cached_tokens(self) -> None:
        data = parse_responses_turn(
            SimpleNamespace(
                model="gpt-5.6-luna",
                status="incomplete",
                error=None,
                incomplete_details={"reason": "content_filter"},
                output_text="",
                output=[
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Policy check."}],
                        "encrypted_content": "encrypted",
                    },
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "refusal", "refusal": "I can’t help with that."}
                        ],
                    },
                ],
                usage={
                    "input_tokens": 100,
                    "output_tokens": 12,
                    "total_tokens": 112,
                    "input_tokens_details": {"cached_tokens": 64},
                    "output_tokens_details": {"reasoning_tokens": 8},
                },
            ),
            requested_model="gpt-5.6-luna",
        )

        self.assertEqual(data.text, "I can’t help with that.")
        self.assertEqual(data.refusal, "I can’t help with that.")
        self.assertEqual(data.reasoning_content, "Policy check.")
        self.assertEqual(data.incomplete_details, {"reason": "content_filter"})
        self.assertEqual(data.finish_reason, "content_filter")
        self.assertEqual(data.cached_prompt_tokens, 64)
        self.assertEqual(data.reasoning_tokens, 8)

    def test_api_level_failure_is_recorded_as_error_then_fails_over(self) -> None:
        settings = _settings(openai_chat_model_fallbacks=("gpt-5.6-luna-backup",))
        client = OpenAIClient(settings)
        calls: list[dict[str, object]] = []

        async def fake_create(request_kwargs, **_options):
            calls.append(request_kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    model="gpt-5.6-luna",
                    status="failed",
                    error=SimpleNamespace(code="server_error", message="Server error."),
                    output=[],
                )
            return SimpleNamespace(
                model="gpt-5.6-luna-backup",
                status="completed",
                error=None,
                output_text="Recovered.",
                output=[],
                usage=SimpleNamespace(input_tokens=4, output_tokens=2, total_tokens=6),
            )

        client._create_response = fake_create
        turn = asyncio.run(
            client.complete_chat_turn(
                model="gpt-5.6-luna",
                feature="chat_reply",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=700,
                temperature=0.7,
            )
        )

        self.assertEqual([call["model"] for call in calls], ["gpt-5.6-luna", "gpt-5.6-luna-backup"])
        self.assertEqual([attempt.status for attempt in turn.provider_attempts], ["error", "ok"])
        self.assertIn("server_error", turn.provider_attempts[0].error)
        self.assertEqual(turn.text, "Recovered.")


def _settings(**overrides):
    values = {
        "openai_api_key": "test-key",
        "openai_base_url": None,
        "openai_chat_model": "gpt-5.6-luna",
        "openai_chat_model_fallbacks": (),
        "openai_memory_model": "gpt-5.6-luna",
        "openai_vision_model": "gpt-5.6-luna",
        "openai_reasoning_effort": "high",
        "openai_efficiency_reasoning_effort": "minimal",
        "openai_embedding_api_key": None,
        "openai_embedding_base_url": None,
        "openai_fallback_api_key": None,
        "openai_fallback_base_url": None,
        "openai_fallback_chat_model": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _web_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "web",
            "description": "Search",
            "parameters": {"type": "object"},
        },
    }


if __name__ == "__main__":
    unittest.main()
