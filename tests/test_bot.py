import unittest

from nycti.formatting import (
    append_debug_block,
    extract_sec_query,
    extract_search_query,
    format_latency_debug_block,
    format_ping_message,
    render_custom_emoji_aliases,
    strip_think_blocks,
)


class BotUtilitiesTests(unittest.TestCase):
    def test_format_ping_message_rounds_to_milliseconds(self) -> None:
        self.assertEqual(format_ping_message(0.1234), "Pong! `123 ms`")

    def test_format_ping_message_clamps_negative_latency(self) -> None:
        self.assertEqual(format_ping_message(-1.0), "Pong! `0 ms`")

    def test_format_latency_debug_block_contains_expected_keys(self) -> None:
        block = format_latency_debug_block(
            {
                "chat_model": "gpt-4.1-mini",
                "memory_model": "gpt-4.1-nano",
                "end_to_end_ms": 1000,
                "context_fetch_ms": 40,
                "memory_retrieval_ms": 30,
                "chat_llm_ms": 800,
                "chat_usage_write_ms": 5,
                "chat_commit_ms": 10,
                "reply_generation_ms": 900,
            }
        )
        self.assertIn("latency_debug_ms", block)
        self.assertIn("chat_model: gpt-4.1-mini", block)
        self.assertIn("memory_model: gpt-4.1-nano", block)
        self.assertIn("end_to_end_ms: 1000", block)
        self.assertIn("memory_extraction: background", block)

    def test_append_debug_block_trims_reply_to_limit(self) -> None:
        reply = "x" * 1900
        debug_block = "```text\nsample\n```"
        merged = append_debug_block(reply, debug_block, limit=1900)
        self.assertLessEqual(len(merged), 1900)
        self.assertIn("sample", merged)

    def test_strip_think_blocks_removes_reasoning_wrapper(self) -> None:
        text = "<think>internal reasoning</think>\n\nmorning mat! :wave:"
        self.assertEqual(strip_think_blocks(text), "morning mat! :wave:")

    def test_strip_think_blocks_handles_missing_blocks(self) -> None:
        text = "hello"
        self.assertEqual(strip_think_blocks(text), "hello")

    def test_render_custom_emoji_aliases_replaces_known_aliases(self) -> None:
        text = "this is scuffed :pepebeat: and funny :kekw:"
        rendered = render_custom_emoji_aliases(
            text,
            {"pepebeat": "<:pepebeat:111>", "kekw": "<:kekw:222>"},
        )
        self.assertEqual(rendered, "this is scuffed <:pepebeat:111> and funny <:kekw:222>")

    def test_render_custom_emoji_aliases_leaves_unknown_aliases(self) -> None:
        text = "hmm :unknown:"
        rendered = render_custom_emoji_aliases(text, {"pepeww": "<:pepeww:333>"})
        self.assertEqual(rendered, "hmm :unknown:")

    def test_extract_search_query_detects_exact_phrase(self) -> None:
        requested, query = extract_search_query("use search latest msft earnings")
        self.assertTrue(requested)
        self.assertEqual(query, "latest msft earnings")

    def test_extract_search_query_no_phrase(self) -> None:
        requested, query = extract_search_query("latest msft earnings")
        self.assertFalse(requested)
        self.assertEqual(query, "latest msft earnings")

    def test_extract_sec_query_detects_exact_phrase(self) -> None:
        requested, query = extract_sec_query("use sec latest aapl 10-q")
        self.assertTrue(requested)
        self.assertEqual(query, "latest aapl 10-q")

    def test_extract_sec_query_no_phrase(self) -> None:
        requested, query = extract_sec_query("latest aapl 10-q")
        self.assertFalse(requested)
        self.assertEqual(query, "latest aapl 10-q")


if __name__ == "__main__":
    unittest.main()
