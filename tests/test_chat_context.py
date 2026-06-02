import unittest

from nycti.chat.context import (
    ChatContextBuilder,
    build_related_memory_query,
    build_user_prompt,
    format_channel_alias_block,
    format_member_alias_block,
    format_memories_block,
    format_personal_profile_block,
    format_related_memories_block,
    select_related_memory_user_ids,
    should_include_channel_aliases_for_prompt,
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

    def test_should_include_channel_aliases_only_for_cross_channel_send_requests(self) -> None:
        self.assertTrue(
            should_include_channel_aliases_for_prompt(
                prompt="post this in alerts: deploy is live",
                context_text="",
            )
        )
        self.assertTrue(
            should_include_channel_aliases_for_prompt(
                prompt="can you send a note to the channel?",
                context_text="",
            )
        )
        self.assertFalse(
            should_include_channel_aliases_for_prompt(
                prompt="remind me tomorrow to check alerts",
                context_text="",
            )
        )
        self.assertFalse(
            should_include_channel_aliases_for_prompt(
                prompt="what happened in chat earlier?",
                context_text="",
            )
        )

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
        self.assertIn("Treat the short personal profile as optional background", rendered)
        self.assertIn("use `channel_ctx` instead of guessing", rendered)
        self.assertNotIn("Available tools:", rendered)
        self.assertNotIn("`quote(symbol)`", rendered)
        self.assertIn("The provided local date/time is authoritative.", rendered)
        self.assertIn("Extended channel context:\n- older context summary", rendered)
        self.assertIn("Treat returned older context as lower-priority background.", rendered)
        self.assertIn("Do not paste transcripts or exhaustive message lists", rendered)
        self.assertIn("Included image context:\n- image 1: recent context from Lucis", rendered)
        self.assertIn("Image analysis:\nimage 1 shows a person next to a car", rendered)
        self.assertIn("The user included `use search`", rendered)
        self.assertIn("Prefer one strong search/query first", rendered)

    def test_build_user_prompt_omits_empty_placeholder_sections(self) -> None:
        rendered = build_user_prompt(
            user_name="mat",
            user_id=123,
            user_global_name="matthew",
            owner_context="Current user is not the configured bot owner/admin.",
            current_datetime_text="2026-03-19 19:00:00 PDT",
            prompt="what do you think?",
            context_block="(no recent context)",
            extended_context_block="(not requested yet; use `channel_ctx` if older Discord context is needed)",
            image_context_block="(no included images)",
            vision_context_block="(no image analysis)",
            personal_profile_block="(none)",
            memories_block="(none)",
            channel_alias_block="(none configured)",
            member_alias_block="(none matched)",
            mentioned_user_memories_block="(none)",
        )

        self.assertIn("Current request:\nwhat do you think?", rendered)
        self.assertNotIn("Recent channel context:", rendered)
        self.assertNotIn("Extended channel context:", rendered)
        self.assertNotIn("Included image context:", rendered)
        self.assertNotIn("Image analysis:", rendered)
        self.assertNotIn("Calling user's short personal profile:", rendered)
        self.assertNotIn("Relevant long-term memories:", rendered)
        self.assertNotIn("Known channel aliases:", rendered)
        self.assertNotIn("Relevant member nicknames/aliases:", rendered)
        self.assertNotIn("Relevant memories for mentioned users:", rendered)
        self.assertNotIn("If the current request includes image attachments", rendered)
        self.assertNotIn("When asked to summarize chat or channel history", rendered)
        self.assertNotIn("Treat the short personal profile", rendered)


class _FakeMemoryService:
    async def get_timezone_name(self, session, user_id: int):  # type: ignore[no-untyped-def]
        return "UTC"

    async def is_enabled(self, session, user_id: int):  # type: ignore[no-untyped-def]
        return False


class _FakeChannelAliasService:
    def __init__(self) -> None:
        self.list_calls = 0

    async def list_aliases(self, session, *, guild_id: int):  # type: ignore[no-untyped-def]
        self.list_calls += 1
        return [type("Alias", (), {"alias": "alerts", "channel_id": 123})()]


class _FakeMemberAliasService:
    async def list_matching_aliases(self, session, *, guild_id: int, text: str):  # type: ignore[no-untyped-def]
        return []


class ChatContextBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_skips_channel_alias_lookup_without_send_hint(self) -> None:
        channel_alias_service = _FakeChannelAliasService()
        builder = ChatContextBuilder(
            memory_service=_FakeMemoryService(),
            channel_alias_service=channel_alias_service,
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="remind me tomorrow to check deploy",
            context_text="",
            include_memories=False,
        )

        self.assertEqual(channel_alias_service.list_calls, 0)
        self.assertEqual(prepared.channel_alias_block, "(none configured)")

    async def test_prepare_includes_channel_aliases_for_send_hint(self) -> None:
        channel_alias_service = _FakeChannelAliasService()
        builder = ChatContextBuilder(
            memory_service=_FakeMemoryService(),
            channel_alias_service=channel_alias_service,
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="post deploy is live in alerts",
            context_text="",
            include_memories=False,
        )

        self.assertEqual(channel_alias_service.list_calls, 1)
        self.assertIn("alerts: channel_id=123", prepared.channel_alias_block)


if __name__ == "__main__":
    unittest.main()
