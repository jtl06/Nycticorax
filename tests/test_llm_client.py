import unittest
import sys
import types

fake_openai = types.ModuleType("openai")


class AsyncOpenAI:  # pragma: no cover - import shim for unit tests
    pass


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from nycti.llm.client import _build_chat_completion_request, _extract_inline_tool_calls


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


if __name__ == "__main__":
    unittest.main()
