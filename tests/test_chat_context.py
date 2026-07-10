from datetime import datetime, timezone
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
    should_include_datetime_for_prompt,
    should_retrieve_memories_for_prompt,
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
            mentioned_user_ids=[789],
            member_aliases=[type("Alias", (), {"user_id": 456})()],
        )
        self.assertEqual(selected, [789, 456])

    def test_select_related_memory_user_ids_ignores_unstructured_text(self) -> None:
        selected = select_related_memory_user_ids(
            current_user_id=123,
            mentioned_user_ids=[],
            member_aliases=[],
        )

        self.assertEqual([], selected)

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
        self.assertFalse(
            should_include_channel_aliases_for_prompt(
                prompt="tell him the truth and say hello to mat",
                context_text="",
            )
        )

    def test_build_user_prompt_keeps_context_but_not_duplicate_tool_instructions(self) -> None:
        rendered = build_user_prompt(
            user_name="mat",
            user_id=123,
            user_global_name="matthew",
            owner_context="Current user is the configured bot owner/admin.",
            current_datetime_text="2026-03-19 19:00:00 PDT",
            prompt="verify the latest nvda earnings",
            context_block="(no recent context)",
            extended_context_block="- older context summary",
            image_context_block="- image 1: recent context from Lucis",
            vision_context_block="image 1 shows a person next to a car",
            personal_profile_block="- likes direct answers",
            memories_block="(none)",
            channel_alias_block="(none configured)",
            member_alias_block="- GTS: user_id=456 (plays ranked)",
            mentioned_user_memories_block="- user_id=456 [preference] Likes ranked.",
        )
        self.assertIn("Owner/admin context:\nCurrent user is the configured bot owner/admin.", rendered)
        self.assertIn("Current request:\nverify the latest nvda earnings", rendered)
        self.assertIn("Calling user's short personal profile:\n- likes direct answers", rendered)
        self.assertIn("Relevant member nicknames/aliases:\n- GTS: user_id=456 (plays ranked)", rendered)
        self.assertIn("Relevant memories for mentioned users:\n- user_id=456 [preference] Likes ranked.", rendered)
        self.assertIn("Treat the short personal profile as optional background", rendered)
        self.assertNotIn("use `channel_ctx` instead of guessing", rendered)
        self.assertNotIn("Available tools:", rendered)
        self.assertNotIn("`quote(symbol)`", rendered)
        self.assertNotIn("The provided local date/time is authoritative.", rendered)
        self.assertIn("Extended channel context:\n- older context summary", rendered)
        self.assertIn("Treat returned older context as lower-priority background.", rendered)
        self.assertIn("Do not paste transcripts or exhaustive message lists", rendered)
        self.assertIn("Included image context:\n- image 1: recent context from Lucis", rendered)
        self.assertIn("Image analysis:\nimage 1 shows a person next to a car", rendered)
        self.assertNotIn("The user included `use search`", rendered)
        self.assertNotIn("Prefer one strong search/query first", rendered)

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

    def test_datetime_context_is_gated_by_request_relevance(self) -> None:
        self.assertFalse(should_include_datetime_for_prompt("tell me a joke"))
        self.assertTrue(should_include_datetime_for_prompt("what is NVDA trading at now?"))
        self.assertTrue(should_include_datetime_for_prompt("remind me tomorrow"))

    def test_memory_retrieval_is_gated_by_personal_relevance(self) -> None:
        self.assertFalse(
            should_retrieve_memories_for_prompt(
                prompt="what is the capital of France?",
                context_text="",
            )
        )
        self.assertTrue(
            should_retrieve_memories_for_prompt(
                prompt="what keyboard should I get for my setup?",
                context_text="",
            )
        )


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


class _TrackingMemoryService:
    def __init__(self) -> None:
        self.timezone_calls = 0
        self.profile_calls = 0
        self.embedding_calls = 0
        self.own_embeddings: list[object] = []
        self.related_embeddings: list[object] = []
        self.embedding = [1.0, 0.5]

    async def get_timezone_name(self, session, user_id: int):  # type: ignore[no-untyped-def]
        self.timezone_calls += 1
        return "UTC"

    async def is_enabled(self, session, user_id: int):  # type: ignore[no-untyped-def]
        return True

    async def get_personal_profile_md(self, session, user_id: int):  # type: ignore[no-untyped-def]
        self.profile_calls += 1
        return "- likes keyboards"

    async def build_retrieval_query_embedding(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.embedding_calls += 1
        return self.embedding

    async def retrieve_relevant(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.own_embeddings.append(kwargs["query_embedding"])
        return []

    async def retrieve_relevant_for_users(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.related_embeddings.append(kwargs["query_embedding"])
        return {}


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

    async def test_prepare_always_includes_date_but_skips_optional_profile_and_memory(self) -> None:
        memory_service = _TrackingMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what is the capital of France?",
            context_text="",
            include_memories=True,
            now=datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual("Friday, July 10, 2026", prepared.current_datetime_text)
        self.assertEqual(1, memory_service.timezone_calls)
        self.assertEqual(0, memory_service.profile_calls)
        self.assertEqual(0, memory_service.embedding_calls)

    async def test_prepare_reuses_one_embedding_for_caller_and_related_users(self) -> None:
        memory_service = _TrackingMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what should I get for my setup with user_id=789?",
            context_text="",
            include_memories=True,
            mentioned_user_ids=[789],
        )

        self.assertEqual(1, memory_service.embedding_calls)
        self.assertIs(memory_service.own_embeddings[0], memory_service.embedding)
        self.assertIs(memory_service.related_embeddings[0], memory_service.embedding)


if __name__ == "__main__":
    unittest.main()
