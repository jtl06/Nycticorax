import unittest

from nycti.chat.context import (
    build_related_memory_query,
    build_user_prompt,
    format_channel_alias_block,
    format_member_alias_block,
    format_memories_block,
    format_personal_profile_block,
    format_related_memories_block,
    select_related_memory_user_ids,
)


class ChatContextTests(unittest.TestCase):
    def test_format_memories_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_memories_block([]), "(none)")

    def test_format_channel_alias_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_channel_alias_block([]), "(none configured)")

    def test_format_member_alias_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_member_alias_block([]), "(none matched)")

    def test_format_personal_profile_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_personal_profile_block("  "), "(none)")

    def test_format_related_memories_block_groups_by_user_id(self) -> None:
        rendered = format_related_memories_block(
            {
                456: [
                    type("Memory", (), {"category": "preference", "summary": "Prefers ranked."})(),
                    type("Memory", (), {"category": "plan", "summary": "Is working on a build."})(),
                    type("Memory", (), {"category": "extra", "summary": "Should be capped."})(),
                ]
            }
        )
        self.assertIn("user_id=456 [preference] Prefers ranked.", rendered)
        self.assertIn("user_id=456 [plan] Is working on a build.", rendered)
        self.assertNotIn("Should be capped", rendered)

    def test_select_related_memory_user_ids_uses_mentions_and_aliases(self) -> None:
        selected = select_related_memory_user_ids(
            current_user_id=123,
            prompt="what about @gts81 (user_id=456)",
            context_text="mat mentioned @foo (user_id=789)",
            member_aliases=[type("Alias", (), {"user_id": 456})()],
        )
        self.assertEqual(selected, [456, 789])

    def test_build_related_memory_query_includes_alias_user_id_mapping(self) -> None:
        rendered = build_related_memory_query(
            prompt="what about gts",
            member_aliases=[type("Alias", (), {"alias": "GTS", "user_id": 456})()],
        )
        self.assertIn("GTS=user_id=456", rendered)

    def test_build_user_prompt_includes_required_search_instruction(self) -> None:
        rendered = build_user_prompt(
            user_name="mat",
            user_id=123,
            user_global_name="matthew",
            owner_context="Current user is the configured bot owner/admin.",
            current_datetime_text="2026-03-19 19:00:00 PDT",
            prompt="latest nvda earnings use search",
            context_block="(no recent context)",
            extended_context_block="- older context summary",
            image_context_block="- image 1: recent context from Lucis",
            vision_context_block="image 1 shows a person next to a car",
            personal_profile_block="- likes direct answers",
            memories_block="(none)",
            channel_alias_block="(none configured)",
            member_alias_block="- GTS: user_id=456 (plays ranked)",
            mentioned_user_memories_block="- user_id=456 [preference] Likes ranked.",
            search_requested=True,
        )
        self.assertIn("Owner/admin context:\nCurrent user is the configured bot owner/admin.", rendered)
        self.assertIn("Current request:\nlatest nvda earnings use search", rendered)
        self.assertIn("Calling user's short personal profile:\n- likes direct answers", rendered)
        self.assertIn("Relevant member nicknames/aliases:\n- GTS: user_id=456 (plays ranked)", rendered)
        self.assertIn("Relevant memories for mentioned users:\n- user_id=456 [preference] Likes ranked.", rendered)
        self.assertIn("Treat the short personal profile as compact background", rendered)
        self.assertIn("use `get_channel_context` rather than guessing", rendered)
        self.assertNotIn("Available tools:", rendered)
        self.assertNotIn("`stock_quote(symbol)`", rendered)
        self.assertIn("The provided current local date/time above is authoritative.", rendered)
        self.assertIn("Extended channel context:\n- older context summary", rendered)
        self.assertIn("Treat any older channel context returned by the tool as lower-priority background.", rendered)
        self.assertIn("Do not paste a transcript or list every message", rendered)
        self.assertIn("Included image context:\n- image 1: recent context from Lucis", rendered)
        self.assertIn("Image analysis:\nimage 1 shows a person next to a car", rendered)
        self.assertIn("The user included `use search`", rendered)
        self.assertIn("Prefer one strong search query", rendered)


if __name__ == "__main__":
    unittest.main()
