import asyncio
from types import SimpleNamespace
import unittest

from nycti.llm.client import OpenAIClient
from nycti.llm.responses_adapter import build_responses_request


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


if __name__ == "__main__":
    unittest.main()
