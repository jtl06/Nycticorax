import unittest
import sys
import types

fake_openai = types.ModuleType("openai")


class AsyncOpenAI:  # pragma: no cover - import shim for unit tests
    pass


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from nycti.llm.client import _extract_inline_tool_calls


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
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_sec_filings",
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
                "<|tool_call_begin|> call_2 lookup_sec_filings <|tool_call_argument_begin|> "
                '{"query": "latest 10-Q for MU"} '
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
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_sec_filings",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        )
        self.assertEqual(text, "before\n\nafter")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "lookup_sec_filings")


if __name__ == "__main__":
    unittest.main()
