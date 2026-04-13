import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from nycti.message_context import (
    MessageContextCollector,
    clean_trigger_content,
    contains_named_trigger,
    dedupe_image_refs,
    dedupe_lines,
    format_message_line,
    image_refs_for_message,
    message_has_visible_content,
)


class _FakeHistoryChannel:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages

    async def history(self, *, limit: int, before: object | None, oldest_first: bool):  # type: ignore[no-untyped-def]
        selected = list(reversed(self.messages))[:limit]
        if oldest_first:
            selected = list(reversed(selected))
        for item in selected:
            yield item


class MessageContextHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_trigger_content_removes_bot_mentions(self) -> None:
        message = SimpleNamespace(content="<@123> hey <@!123> can you check this")
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=123),
            "hey can you check this",
        )

    def test_clean_trigger_content_removes_named_trigger_word(self) -> None:
        message = SimpleNamespace(content="nycti, can you check this")
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=None),
            "can you check this",
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


if __name__ == "__main__":
    unittest.main()
