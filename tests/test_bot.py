import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from nycti.discord.help import format_help_message
from nycti.formatting import (
    append_debug_block,
    build_multimodal_user_content,
    extract_image_attachment_urls,
    parse_discord_message_links,
    extract_search_query,
    extract_think_content,
    format_channel_alias_list,
    format_discord_message_link,
    format_current_datetime_context,
    format_latency_debug_block,
    format_ping_message,
    format_reminder_list,
    format_thinking_block,
    normalize_discord_tables,
    parse_json_object_payload,
    parse_query_list_payload,
    render_custom_emoji_aliases,
    split_message_chunks,
    strip_think_blocks,
)


class BotUtilitiesTests(unittest.TestCase):
    def test_format_ping_message_rounds_to_milliseconds(self) -> None:
        self.assertEqual(format_ping_message(0.1234), "Pong! `123 ms`")

    def test_format_ping_message_clamps_negative_latency(self) -> None:
        self.assertEqual(format_ping_message(-1.0), "Pong! `0 ms`")

    def test_extract_image_attachment_urls_filters_non_images_and_limits_count(self) -> None:
        attachments = [
            SimpleNamespace(content_type="image/png", filename="chart.png", url="https://cdn.example.com/a.png"),
            SimpleNamespace(content_type="text/plain", filename="notes.txt", url="https://cdn.example.com/notes.txt"),
            SimpleNamespace(content_type="", filename="photo.jpeg", url="https://cdn.example.com/b.jpeg"),
            SimpleNamespace(content_type="image/webp", filename="meme.webp", url="https://cdn.example.com/c.webp"),
            SimpleNamespace(content_type="image/gif", filename="clip.gif", url="https://cdn.example.com/d.gif"),
        ]
        self.assertEqual(
            extract_image_attachment_urls(attachments),
            [
                "https://cdn.example.com/a.png",
                "https://cdn.example.com/b.jpeg",
                "https://cdn.example.com/c.webp",
            ],
        )

    def test_build_multimodal_user_content_wraps_text_and_images(self) -> None:
        content = build_multimodal_user_content("look at this chart", ["https://cdn.example.com/chart.png"])
        self.assertIsInstance(content, list)
        assert isinstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "look at this chart"})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "https://cdn.example.com/chart.png"}},
        )

    def test_parse_discord_message_links_extracts_same_guild_links(self) -> None:
        text = (
            "look at this https://discord.com/channels/123/456/789 and "
            "https://canary.discord.com/channels/123/456/790"
        )
        self.assertEqual(parse_discord_message_links(text, guild_id=123), [(456, 789), (456, 790)])

    def test_parse_discord_message_links_ignores_other_guilds_and_dedupes(self) -> None:
        text = (
            "https://discord.com/channels/999/456/789 "
            "https://discord.com/channels/123/456/789 "
            "https://discord.com/channels/123/456/789"
        )
        self.assertEqual(parse_discord_message_links(text, guild_id=123), [(456, 789)])

    def test_format_help_message_mentions_core_commands_and_tips(self) -> None:
        help_page_one = format_help_message(1)
        help_page_two = format_help_message(2)
        self.assertIn("/help page:<1-2>", help_page_one)
        self.assertIn("/ping", help_page_one)
        self.assertIn("/memory enable:<true|false>", help_page_one)
        self.assertIn("/memory forget:<id>", help_page_one)
        self.assertIn("use search", help_page_two)
        self.assertTrue(all(len(page) <= 2000 for page in (help_page_one, help_page_two)))

    def test_format_latency_debug_block_contains_expected_keys(self) -> None:
        block = format_latency_debug_block(
            {
                "chat_model": "gpt-4.1-mini",
                "vision_model": "gpt-4.1-vision",
                "active_chat_model": "gpt-4.1-vision",
                "memory_model": "gpt-4.1-nano",
                "chat_prompt_tokens": 1200,
                "chat_completion_tokens": 300,
                "chat_total_tokens": 1500,
                "end_to_end_ms": 1000,
                "context_fetch_ms": 40,
                "memory_retrieval_ms": 30,
                "tool_call_count": 3,
                "web_search_query_count": 2,
                "web_search_ms": 120,
                "chat_llm_ms": 800,
                "chat_usage_write_ms": 5,
                "chat_commit_ms": 10,
                "reply_generation_ms": 900,
            }
        )
        self.assertIn("latency_debug_ms", block)
        self.assertIn("chat_model: gpt-4.1-mini", block)
        self.assertIn("vision_model: gpt-4.1-vision", block)
        self.assertIn("active_chat_model: gpt-4.1-vision", block)
        self.assertIn("memory_model: gpt-4.1-nano", block)
        self.assertIn("chat_prompt_tokens: 1200", block)
        self.assertIn("chat_completion_tokens: 300", block)
        self.assertIn("chat_total_tokens: 1500", block)
        self.assertIn("chat_tokens_per_s: 375.0", block)
        self.assertIn("end_to_end_ms: 1000", block)
        self.assertIn("tool_call_count: 3", block)
        self.assertIn("web_search_query_count: 2", block)
        self.assertIn("memory_extraction: background", block)

    def test_append_debug_block_trims_reply_to_limit(self) -> None:
        reply = "x" * 1900
        debug_block = "```text\nsample\n```"
        merged = append_debug_block(reply, debug_block, limit=1900)
        self.assertLessEqual(len(merged), 1900)
        self.assertIn("sample", merged)

    def test_append_debug_block_can_skip_trimming(self) -> None:
        reply = "x" * 1900
        debug_block = "```text\nsample\n```"
        merged = append_debug_block(reply, debug_block, limit=None)
        self.assertGreater(len(merged), 1900)
        self.assertTrue(merged.endswith(debug_block))

    def test_split_message_chunks_keeps_all_content(self) -> None:
        text = ("alpha\n\n" + ("x" * 1500) + "\n\n" + ("y" * 1500)).strip()
        chunks = split_message_chunks(text, limit=1900)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text.replace("\n\n", ""))

    def test_strip_think_blocks_removes_reasoning_wrapper(self) -> None:
        text = "<think>internal reasoning</think>\n\nmorning mat! :wave:"
        self.assertEqual(strip_think_blocks(text), "morning mat! :wave:")

    def test_strip_think_blocks_handles_missing_blocks(self) -> None:
        text = "hello"
        self.assertEqual(strip_think_blocks(text), "hello")

    def test_extract_think_content_collects_multiple_blocks(self) -> None:
        text = "<think>first</think>\nhello\n<think>second</think>"
        self.assertEqual(extract_think_content(text), ["first", "second"])

    def test_format_thinking_block_quotes_reasoning(self) -> None:
        block = format_thinking_block(["step one", "step two"])
        self.assertIn("-# reasoning", block)
        self.assertIn("> step one", block)
        self.assertIn("> step two", block)

    def test_normalize_discord_tables_wraps_markdown_table_in_code_block(self) -> None:
        text = "| Name | Revenue |\n| --- | --- |\n| NVDA | $39.3B |\n| AMD | $7.7B |"
        normalized = normalize_discord_tables(text)
        self.assertTrue(normalized.startswith("```text\n"))
        self.assertIn("Name | Revenue", normalized)
        self.assertIn("NVDA | $39.3B", normalized)
        self.assertIn("-+-", normalized)

    def test_normalize_discord_tables_leaves_normal_text_unchanged(self) -> None:
        text = "Revenue was strong.\nGuidance was mixed."
        self.assertEqual(normalize_discord_tables(text), text)

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

    def test_format_current_datetime_context_includes_localized_date_time(self) -> None:
        rendered = format_current_datetime_context(
            datetime(2026, 3, 19, 20, 34, 56, tzinfo=timezone.utc),
            "America/Los_Angeles",
        )
        self.assertEqual(rendered, "2026-03-19 13:34:56 PDT")

    def test_format_discord_message_link_uses_guild_channel_and_message_ids(self) -> None:
        link = format_discord_message_link(guild_id=123, channel_id=456, message_id=789)
        self.assertEqual(link, "https://discord.com/channels/123/456/789")

    def test_format_reminder_list_renders_jump_link(self) -> None:
        reminder = SimpleNamespace(
            id=12,
            guild_id=123,
            channel_id=456,
            user_id=789,
            source_message_id=321,
            remind_at=datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc),
            reminder_text="check NVDA earnings",
        )
        rendered = format_reminder_list([reminder], timezone_name="America/Los_Angeles")
        self.assertIn("`12`", rendered)
        self.assertIn("check NVDA earnings", rendered)
        self.assertIn("https://discord.com/channels/123/456/321", rendered)

    def test_format_reminder_list_can_include_owner_and_channel(self) -> None:
        reminder = SimpleNamespace(
            id=13,
            guild_id=123,
            channel_id=456,
            user_id=789,
            source_message_id=None,
            remind_at=datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc),
            reminder_text="roll the calls",
        )
        rendered = format_reminder_list([reminder], timezone_name="UTC", include_owner=True)
        self.assertIn("<@789>", rendered)
        self.assertIn("<#456>", rendered)

    def test_format_channel_alias_list_renders_aliases(self) -> None:
        alias = SimpleNamespace(alias="alerts", channel_id=456)
        rendered = format_channel_alias_list([alias])
        self.assertEqual(rendered, "`alerts` -> <#456> (`456`)")

    def test_parse_query_list_payload_uses_queries_from_json(self) -> None:
        parsed = parse_query_list_payload('{"queries": ["micron earnings", "nvidia guidance"]}', fallback="fallback")
        self.assertEqual(parsed, ["micron earnings", "nvidia guidance"])

    def test_parse_json_object_payload_handles_embedded_json(self) -> None:
        parsed = parse_json_object_payload('noise {"query": "latest nvda earnings"} trailing')
        self.assertEqual(parsed, {"query": "latest nvda earnings"})

    def test_parse_json_object_payload_rejects_non_object(self) -> None:
        parsed = parse_json_object_payload('["latest nvda earnings"]')
        self.assertIsNone(parsed)

    def test_parse_query_list_payload_falls_back_when_json_is_invalid(self) -> None:
        parsed = parse_query_list_payload("not json", fallback="fallback query")
        self.assertEqual(parsed, ["fallback query"])

    def test_parse_query_list_payload_dedupes_and_limits_queries(self) -> None:
        parsed = parse_query_list_payload(
            '{"queries": ["Micron", "micron", " Nvidia ", "AMD", "TSMC"]}',
            fallback="fallback",
        )
        self.assertEqual(parsed, ["Micron", "Nvidia", "AMD"])

    def test_extract_search_query_detects_exact_phrase(self) -> None:
        requested, query = extract_search_query("use search latest msft earnings")
        self.assertTrue(requested)
        self.assertEqual(query, "latest msft earnings")

    def test_extract_search_query_no_phrase(self) -> None:
        requested, query = extract_search_query("latest msft earnings")
        self.assertFalse(requested)
        self.assertEqual(query, "latest msft earnings")


if __name__ == "__main__":
    unittest.main()
