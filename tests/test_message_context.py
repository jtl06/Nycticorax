import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from nycti.message_context import (
    MessageContextCollector,
    clean_trigger_content,
    contains_named_trigger,
    dedupe_image_refs,
    dedupe_lines,
    expand_user_mentions,
    format_message_line,
    image_refs_for_message,
    message_has_visible_content,
)


class _FakeHistoryChannel:
    def __init__(self, messages: list[object], *, resolved_messages: dict[int, object] | None = None) -> None:
        self.messages = messages
        self.resolved_messages = resolved_messages or {}
        for message in self.messages:
            if getattr(message, "channel", None) is None:
                message.channel = self

    async def history(
        self,
        *,
        limit: int,
        before: object | None = None,
        after: object | None = None,
        oldest_first: bool,
    ):  # type: ignore[no-untyped-def]
        selected = list(self.messages)
        if before is not None:
            before_id = getattr(before, "id", before)
            if isinstance(before_id, int):
                selected = [item for item in selected if getattr(item, "id", -1) < before_id]
        if after is not None:
            after_id = getattr(after, "id", after)
            if isinstance(after_id, int):
                selected = [item for item in selected if getattr(item, "id", -1) > after_id]
        if not oldest_first:
            selected = list(reversed(selected))
        selected = selected[:limit]
        for item in selected:
            yield item

    async def fetch_message(self, message_id: int):  # type: ignore[no-untyped-def]
        if message_id in self.resolved_messages:
            return self.resolved_messages[message_id]
        for message in self.messages:
            if getattr(message, "id", None) == message_id:
                return message
        raise AssertionError(f"Message {message_id} not found in fake channel")


class MessageContextHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_trigger_content_removes_bot_mentions(self) -> None:
        message = SimpleNamespace(content="<@123> hey <@!123> can you check this", mentions=[])
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=123),
            "hey can you check this",
        )

    def test_clean_trigger_content_removes_named_trigger_word(self) -> None:
        message = SimpleNamespace(content="nycti, can you check this", mentions=[])
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=None),
            "can you check this",
        )

    def test_clean_trigger_content_expands_other_user_mentions(self) -> None:
        message = SimpleNamespace(
            content="<@123> what about <@456>",
            mentions=[
                SimpleNamespace(id=123, display_name="Nycti"),
                SimpleNamespace(id=456, display_name="gts81"),
            ],
        )
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=123),
            "what about @gts81 (user_id=456)",
        )

    def test_contains_named_trigger_detects_standalone_word(self) -> None:
        self.assertTrue(contains_named_trigger("hey nycti what do you think"))
        self.assertTrue(contains_named_trigger("Nycti? check SPX"))

    def test_contains_named_trigger_ignores_substrings(self) -> None:
        self.assertFalse(contains_named_trigger("benycti is not a trigger"))

    def test_message_has_visible_content_accepts_attachment_only_messages(self) -> None:
        message = SimpleNamespace(content="   ", attachments=[SimpleNamespace()])
        self.assertTrue(message_has_visible_content(message))

    def test_format_message_line_uses_attachment_placeholder_when_text_is_empty(self) -> None:
        message = SimpleNamespace(
            content="",
            attachments=[SimpleNamespace(), SimpleNamespace()],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(format_message_line(message), "mat: [2 attachment(s)]")

    def test_format_message_line_can_include_timestamp(self) -> None:
        message = SimpleNamespace(
            content="check this",
            attachments=[],
            author=SimpleNamespace(display_name="mat"),
            created_at=datetime(2026, 4, 12, 21, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            format_message_line(message, include_timestamp=True),
            "[2026-04-12 21:05 UTC] mat: check this",
        )

    def test_format_message_line_expands_user_mentions(self) -> None:
        message = SimpleNamespace(
            content="replying to <@!456>",
            attachments=[],
            mentions=[SimpleNamespace(id=456, display_name="gts81")],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(
            format_message_line(message),
            "mat: replying to @gts81 (user_id=456)",
        )

    def test_format_message_line_caps_content_text_to_280_chars_by_default(self) -> None:
        message = SimpleNamespace(
            content="x" * 320,
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="mat"),
        )
        rendered = format_message_line(message)
        self.assertTrue(rendered.startswith("mat: "))
        content = rendered.split("mat: ", 1)[1]
        self.assertEqual(len(content), 280)
        self.assertTrue(content.endswith("..."))

    def test_expand_user_mentions_uses_global_name_fallback(self) -> None:
        rendered = expand_user_mentions(
            "cc <@456>",
            [SimpleNamespace(id=456, display_name="", global_name="Garrett", name="gts81")],
        )
        self.assertEqual(rendered, "cc @Garrett (user_id=456)")

    def test_dedupe_lines_preserves_order(self) -> None:
        self.assertEqual(
            dedupe_lines(["a", "b", "a", "c", "b"]),
            ["a", "b", "c"],
        )

    def test_image_refs_for_message_labels_sources(self) -> None:
        message = SimpleNamespace(
            attachments=[
                SimpleNamespace(
                    content_type="image/png",
                    filename="chart.png",
                    url="https://cdn.example.com/chart.png",
                )
            ],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(
            image_refs_for_message(message, label="current message", image_limit=3),
            [("current message from mat", "https://cdn.example.com/chart.png")],
        )

    def test_dedupe_image_refs_dedupes_by_url_and_respects_cap(self) -> None:
        refs = [
            ("current message from mat", "https://cdn.example.com/a.png"),
            ("reply depth 1 from mat", "https://cdn.example.com/a.png"),
            ("recent context from joe", "https://cdn.example.com/b.png"),
            ("linked message from joe", "https://cdn.example.com/c.png"),
        ]
        self.assertEqual(
            dedupe_image_refs(refs, max_count=2),
            [
                ("current message from mat", "https://cdn.example.com/a.png"),
                ("recent context from joe", "https://cdn.example.com/b.png"),
            ],
        )

    async def test_build_extended_history_context_skips_recent_window(self) -> None:
        messages = [
            SimpleNamespace(
                id=index,
                content=f"message {index}",
                attachments=[],
                author=SimpleNamespace(display_name=f"user{index}"),
                created_at=datetime(2026, 4, 12, 20, index, tzinfo=timezone.utc),
            )
            for index in range(6)
        ]
        current_message = SimpleNamespace(
            channel=_FakeHistoryChannel(messages),
        )
        collector = MessageContextCollector(
            bot=SimpleNamespace(),
            channel_context_limit=2,
            max_reply_chain_depth=0,
            max_linked_message_count=0,
            max_context_image_count=0,
            anchor_context_per_side=1,
        )

        lines = await collector.build_extended_history_context(current_message, limit=3)

        self.assertEqual(
            lines,
            [
                "[2026-04-12 20:01 UTC] user1: message 1",
                "[2026-04-12 20:02 UTC] user2: message 2",
                "[2026-04-12 20:03 UTC] user3: message 3",
            ],
        )

    async def test_build_message_context_keeps_reply_chain_within_limit(self) -> None:
        base_time = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        history_messages = [
            SimpleNamespace(
                id=index,
                content=f"history {index}",
                attachments=[],
                mentions=[],
                author=SimpleNamespace(display_name=f"user{index}"),
                created_at=base_time,
            )
            for index in range(10, 16)
        ]
        replied_to = SimpleNamespace(
            id=1,
            content="important older point",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="gts81"),
            created_at=base_time,
            reference=None,
        )
        current_message = SimpleNamespace(
            id=99,
            content="<@123> reply here",
            attachments=[],
            mentions=[SimpleNamespace(id=123, display_name="Nycti")],
            author=SimpleNamespace(display_name="mat"),
            created_at=base_time,
            reference=SimpleNamespace(message_id=1, resolved=replied_to),
            guild=None,
        )
        current_message.channel = _FakeHistoryChannel(history_messages, resolved_messages={1: replied_to})
        collector = MessageContextCollector(
            bot=SimpleNamespace(),
            channel_context_limit=4,
            max_reply_chain_depth=1,
            max_linked_message_count=0,
            max_context_image_count=0,
            anchor_context_per_side=1,
        )

        context_lines, image_urls, image_context_lines = await collector.build_message_context(current_message)

        self.assertEqual(len(context_lines), 4)
        self.assertIn("reply depth 1", context_lines[0])
        self.assertTrue(any("important older point" in line for line in context_lines))
        self.assertEqual(image_urls, [])
        self.assertEqual(image_context_lines, [])

    async def test_build_message_context_includes_anchor_neighbor_lines(self) -> None:
        base_time = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        anchor_before = SimpleNamespace(
            id=40,
            content="context before anchor",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="lucis"),
            created_at=base_time,
        )
        anchor = SimpleNamespace(
            id=41,
            content="main anchor message",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="gts81"),
            created_at=base_time,
            reference=None,
        )
        anchor_after = SimpleNamespace(
            id=42,
            content="context after anchor",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="mat"),
            created_at=base_time,
        )
        recent_1 = SimpleNamespace(
            id=90,
            content="recent one",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="joe"),
            created_at=base_time,
        )
        recent_2 = SimpleNamespace(
            id=91,
            content="recent two",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="joe"),
            created_at=base_time,
        )
        channel = _FakeHistoryChannel([anchor_before, anchor, anchor_after, recent_1, recent_2], resolved_messages={41: anchor})
        current_message = SimpleNamespace(
            id=99,
            content="<@123> thoughts?",
            attachments=[],
            mentions=[SimpleNamespace(id=123, display_name="Nycti")],
            author=SimpleNamespace(display_name="mat"),
            created_at=base_time,
            reference=SimpleNamespace(message_id=41, resolved=anchor),
            channel=channel,
            guild=None,
        )
        collector = MessageContextCollector(
            bot=SimpleNamespace(),
            channel_context_limit=5,
            max_reply_chain_depth=1,
            max_linked_message_count=0,
            max_context_image_count=0,
            anchor_context_per_side=1,
        )

        context_lines, _, _ = await collector.build_message_context(current_message)

        self.assertTrue(any("main anchor message" in line for line in context_lines))
        self.assertTrue(any("anchor context" in line and "context before anchor" in line for line in context_lines))
        self.assertTrue(any("anchor context" in line and "context after anchor" in line for line in context_lines))
        self.assertTrue(any("recent two" in line for line in context_lines))


if __name__ == "__main__":
    unittest.main()
