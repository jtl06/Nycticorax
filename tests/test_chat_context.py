import unittest

from nycti.chat.context import build_user_prompt, format_channel_alias_block, format_memories_block


class ChatContextTests(unittest.TestCase):
    def test_format_memories_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_memories_block([]), "(none)")

    def test_format_channel_alias_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_channel_alias_block([]), "(none configured)")

    def test_build_user_prompt_includes_required_search_instruction(self) -> None:
        rendered = build_user_prompt(
            user_name="mat",
            user_id=123,
            user_global_name="matthew",
            current_datetime_text="2026-03-19 19:00:00 PDT",
            prompt="latest nvda earnings use search",
            context_block="(no recent context)",
            memories_block="(none)",
            channel_alias_block="(none configured)",
            search_requested=True,
        )
        self.assertIn("Current request:\nlatest nvda earnings use search", rendered)
        self.assertIn("The user included `use search`", rendered)
        self.assertIn("Prefer one strong search query", rendered)


if __name__ == "__main__":
    unittest.main()
