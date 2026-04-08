import unittest
from types import SimpleNamespace

from nycti.message_context import (
    clean_trigger_content,
    dedupe_image_refs,
    dedupe_lines,
    format_message_line,
    image_refs_for_message,
    message_has_visible_content,
)


class MessageContextHelpersTests(unittest.TestCase):
    def test_clean_trigger_content_removes_bot_mentions(self) -> None:
        message = SimpleNamespace(content="<@123> hey <@!123> can you check this")
        self.assertEqual(
            clean_trigger_content(message, bot_user_id=123),
            "hey can you check this",
        )

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


if __name__ == "__main__":
    unittest.main()
