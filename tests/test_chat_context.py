import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from nycti.chat.context import (
    ChatContextBuilder,
    build_related_memory_query,
    build_user_prompt,
    format_channel_alias_block,
    format_member_alias_block,
    format_memories_block,
    format_market_watchlist_block,
    format_personal_profile_block,
    format_related_memories_block,
    select_related_memory_user_ids,
    required_watchlist_symbols_for_request,
    should_include_channel_aliases_for_prompt,
    should_include_datetime_for_prompt,
    should_retrieve_memories_for_prompt,
)


class ChatContextTests(unittest.TestCase):
    def test_format_memories_block_uses_placeholder_when_empty(self) -> None:
        self.assertEqual(format_memories_block([]), "(none)")

    def test_format_memories_block_labels_private_and_guild_ownership(self) -> None:
        rendered = format_memories_block(
            [
                type(
                    "Memory",
                    (),
                    {
                        "user_id": 1,
                        "visibility": "private",
                        "category": "preference",
                        "summary": "Prefers short answers",
                    },
                )(),
                type(
                    "Memory",
                    (),
                    {
                        "user_id": 2,
                        "visibility": "lore",
                        "category": "lore",
                        "summary": "Calls failed deploys moon launches",
                    },
                )(),
            ]
        )

        self.assertIn("[private; fact; preference] Prefers short answers", rendered)
        self.assertIn("[lore; owner_user_id=2; fact; lore]", rendered)

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
        self.assertIn("user_id=456 [fact; preference] Prefers ranked.", rendered)
        self.assertIn("user_id=456 [fact; plan] Is working on a build.", rendered)
        self.assertIn("user_id=456 [fact; extra] Should be capped.", rendered)

    def test_select_related_memory_user_ids_uses_mentions_and_aliases(self) -> None:
        selected = select_related_memory_user_ids(
            current_user_id=123,
            mentioned_user_ids=[789],
            member_aliases=[type("Alias", (), {"user_id": 456})()],
            member_identities=[type("Identity", (), {"user_id": 654})()],
        )
        self.assertEqual(selected, [789, 456, 654])

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
            memories_block="- [private; preference] Prefers direct answers",
            channel_alias_block="(none configured)",
            member_alias_block="- GTS: <@456> (user_id=456; server alias; plays ranked)",
            mentioned_user_memories_block="- user_id=456 [fact; preference] Likes ranked.",
            memory_snapshot_block=(
                "User memory:\n- [fact; preference] Prefers direct answers\n\n"
                "Server memory:\n- [lore; lore; owner_user_id=456] Calls deploys moon launches"
            ),
            market_watchlist_block=(
                "Personal: NVDA, AMD, SNDK\n"
                "Shared market-report defaults: MU, INTC"
            ),
        )
        self.assertIn("Owner/admin context:\nCurrent user is the configured bot owner/admin.", rendered)
        self.assertIn("Current request:\nverify the latest nvda earnings", rendered)
        self.assertIn("Calling user's short personal profile:\n- likes direct answers", rendered)
        self.assertIn(
            "Relevant Discord members and aliases:\n"
            "- GTS: <@456> (user_id=456; server alias; plays ranked)",
            rendered,
        )
        self.assertIn("copying that member's exact `<@...>` token", rendered)
        self.assertIn("This is not a DM or `send_msg` action.", rendered)
        self.assertIn("Relevant memories for mentioned users:\n- user_id=456 [fact; preference] Likes ranked.", rendered)
        self.assertIn("Treat the short personal profile as optional background", rendered)
        self.assertIn("Core memory snapshot:\nUser memory:", rendered)
        self.assertIn("compact warm cache", rendered)
        self.assertIn("Active market watchlist:\nPersonal: NVDA, AMD, SNDK", rendered)
        self.assertIn("canonical typed state", rendered)
        self.assertIn("Do not merely correct the spelling", rendered)
        self.assertIn("Memory entries labeled `private` belong to the current user", rendered)
        self.assertIn("do not attribute them to the current user", rendered)
        self.assertNotIn("use `channel_ctx` instead of guessing", rendered)
        self.assertNotIn("Available tools:", rendered)
        self.assertNotIn("`quote(symbol)`", rendered)
        self.assertNotIn("The provided local date/time is authoritative.", rendered)
        self.assertIn("Extended channel context:\n- older context summary", rendered)
        self.assertIn("Treat returned older context as lower-priority background.", rendered)
        self.assertIn("A short follow-up may continue an unresolved task", rendered)
        self.assertIn("Do not paste transcripts or exhaustive message lists", rendered)
        self.assertIn("Included image context:\n- image 1: recent context from Lucis", rendered)
        self.assertIn("Image analysis:\nimage 1 shows a person next to a car", rendered)
        self.assertNotIn("The user included `use search`", rendered)
        self.assertNotIn("Prefer one strong search/query first", rendered)

    def test_build_user_prompt_preserves_context_url_and_speaker_perspective(self) -> None:
        rendered = build_user_prompt(
            user_name="Lucis",
            user_id=123,
            user_global_name="Lucis",
            owner_context="",
            current_datetime_text="Friday, July 24, 2026",
            prompt="tell mat that i knew",
            context_block="mat: summarize https://example.com/live-event",
            extended_context_block="",
            image_context_block="",
            vision_context_block="",
            personal_profile_block="",
            memories_block="",
            channel_alias_block="",
            member_alias_block="- mat: <@456> (user_id=456)",
            mentioned_user_memories_block="",
        )

        self.assertIn("treat that URL as a supplied input", rendered)
        self.assertIn("rewrite ambiguous first-person pronouns", rendered)
        self.assertIn("caller's displayed name", rendered)

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
        self.assertNotIn("Relevant Discord members and aliases:", rendered)
        self.assertNotIn("Relevant memories for mentioned users:", rendered)
        self.assertNotIn("If the current request includes image attachments", rendered)
        self.assertNotIn("When asked to summarize chat or channel history", rendered)
        self.assertNotIn("Treat the short personal profile", rendered)

    def test_market_watchlist_format_and_complete_request_selection(self) -> None:
        watchlist = SimpleNamespace(
            personal=("NVDA", "AMD", "NVDA"),
            shared=("MU", "SNDK"),
        )

        rendered = format_market_watchlist_block(watchlist)

        self.assertEqual(
            "Personal: NVDA, AMD\nShared market-report defaults: MU, SNDK",
            rendered,
        )
        self.assertEqual(
            ("NVDA", "AMD", "MU", "SNDK"),
            required_watchlist_symbols_for_request(
                "hows stock i care",
                watchlist.symbols if hasattr(watchlist, "symbols") else ("NVDA", "AMD", "MU", "SNDK"),
            ),
        )
        self.assertEqual(
            (),
            required_watchlist_symbols_for_request("NVDA price?", ("NVDA", "AMD")),
        )

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
        self.assertTrue(
            should_retrieve_memories_for_prompt(
                prompt="How are NVDA earnings looking?",
                context_text="",
            )
        )
        self.assertTrue(
            should_retrieve_memories_for_prompt(
                prompt="check $amd",
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

    async def list_matching_identities(self, session, *, guild_id: int, text: str):  # type: ignore[no-untyped-def]
        return []


class _MatchingMemberAliasService(_FakeMemberAliasService):
    async def list_matching_identities(self, session, *, guild_id: int, text: str):  # type: ignore[no-untyped-def]
        if "Lucis" not in text:
            return []
        return [
            type(
                "Identity",
                (),
                {
                    "user_id": 987,
                    "display_name": "Lucis",
                    "global_name": "lucis.global",
                    "username": "lucis_user",
                },
            )()
        ]


class _TrackingMemoryService:
    def __init__(self) -> None:
        self.retriever = type(
            "Retriever",
            (),
            {"settings": type("Settings", (), {"memory_retrieval_limit": 8})()},
        )()
        self.timezone_calls = 0
        self.profile_calls = 0
        self.embedding_calls = 0
        self.own_embeddings: list[object] = []
        self.own_limits: list[int | None] = []
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

    async def get_memory_snapshot_blocks(self, session, *, user_id: int, guild_id: int):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            rendered="User memory:\n- [fact; preference] Likes concise answers",
            source_count=1,
        )

    async def get_active_market_watchlist(self, session, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            personal=("NVDA", "AMD"),
            shared=("MU",),
            symbols=("NVDA", "AMD", "MU"),
        )

    async def build_retrieval_query_embedding(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.embedding_calls += 1
        return self.embedding

    async def retrieve_relevant(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.own_embeddings.append(kwargs["query_embedding"])
        self.own_limits.append(kwargs.get("limit"))
        return []

    async def retrieve_relevant_for_users(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.related_embeddings.append(kwargs["query_embedding"])
        return {}


class _LexicalHitMemoryService(_TrackingMemoryService):
    async def retrieve_relevant(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.own_embeddings.append(kwargs["query_embedding"])
        self.own_limits.append(kwargs.get("limit"))
        return [type("Memory", (), {"category": "preference", "summary": "likes keyboards"})()]


class _UnavailableEmbeddingMemoryService(_LexicalHitMemoryService):
    async def build_retrieval_query_embedding(self, session, **kwargs):
        self.embedding_calls += 1
        return None


class _RelatedHybridMemoryService(_TrackingMemoryService):
    lexical_memory = type(
        "Memory",
        (),
        {"category": "preference", "summary": "user 789 likes keyboards"},
    )()
    semantic_memory = type(
        "Memory",
        (),
        {"category": "project", "summary": "user 790 is building an ergonomic setup"},
    )()

    async def retrieve_relevant_for_users(self, session, **kwargs):
        query_embedding = kwargs["query_embedding"]
        self.related_embeddings.append(query_embedding)
        if query_embedding is None:
            return {789: [self.lexical_memory], 790: []}
        return {789: [self.lexical_memory], 790: [self.semantic_memory]}


class _DisabledRelatedMemoryService(_TrackingMemoryService):
    def __init__(self) -> None:
        super().__init__()
        self.enabled_user_lookups: list[tuple[int, ...]] = []

    async def get_enabled_user_ids(self, session, *, user_ids):  # type: ignore[no-untyped-def]
        self.enabled_user_lookups.append(tuple(user_ids))
        return ()


class _ConcurrentEmbeddingMemoryService(_TrackingMemoryService):
    def __init__(self) -> None:
        super().__init__()
        self.embedding_started = asyncio.Event()
        self.snapshot_started = asyncio.Event()
        self.embedding_usage_records = 0

    async def get_prompt_settings(self, session, user_id: int):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            timezone_name="UTC",
            memory_enabled=True,
            personal_profile_md="- likes keyboards",
        )

    async def get_memory_snapshot_blocks(  # type: ignore[no-untyped-def]
        self,
        session,
        *,
        user_id: int,
        guild_id: int,
        memory_enabled: bool,
    ):
        self.snapshot_started.set()
        await asyncio.wait_for(self.embedding_started.wait(), timeout=1)
        return SimpleNamespace(
            rendered="User memory:\n- [fact; preference] Likes concise answers",
            source_count=1,
        )

    async def generate_retrieval_query_embedding(self, *, query: str):
        self.embedding_started.set()
        await asyncio.wait_for(self.snapshot_started.wait(), timeout=1)
        return SimpleNamespace(embedding=self.embedding, usage=SimpleNamespace())

    async def record_retrieval_query_embedding_usage(self, session, **kwargs):  # type: ignore[no-untyped-def]
        self.embedding_usage_records += 1


class ChatContextBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_records_context_subphase_timings(self) -> None:
        builder = ChatContextBuilder(
            memory_service=_TrackingMemoryService(),
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )
        timings: dict[str, int] = {}

        await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what should I get for my setup?",
            context_text="",
            include_memories=True,
            timing_metrics=timings,
        )

        expected = {
            "ctx_tz_ms",
            "ctx_mem_flag_ms",
            "ctx_profile_ms",
            "ctx_snapshot_ms",
            "ctx_watchlist_ms",
            "ctx_member_alias_ms",
            "ctx_member_ids_ms",
            "ctx_embed_ms",
            "ctx_mem_query_ms",
            "ctx_prepare_format_ms",
            "ctx_prepare_ms",
        }
        self.assertTrue(expected <= timings.keys())
        self.assertTrue(all(timings[key] >= 0 for key in expected))

    async def test_prepare_maps_matching_observed_member_to_discord_mention(self) -> None:
        builder = ChatContextBuilder(
            memory_service=_FakeMemoryService(),
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_MatchingMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="Tell Lucis the build is ready.",
            context_text="",
            include_memories=False,
        )

        self.assertIn("- Lucis: <@987> (user_id=987", prepared.member_alias_block)
        self.assertIn("also lucis.global, lucis_user", prepared.member_alias_block)

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

    async def test_prepare_includes_profile_and_cheap_lexical_memory_on_every_enabled_turn(
        self,
    ) -> None:
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
        self.assertEqual(1, memory_service.profile_calls)
        self.assertEqual(0, memory_service.embedding_calls)
        self.assertEqual([None], memory_service.own_embeddings)
        self.assertEqual([2], memory_service.own_limits)
        self.assertIn("likes keyboards", prepared.personal_profile_block)
        self.assertIn("Likes concise answers", prepared.memory_snapshot_block)
        self.assertEqual(1, prepared.memory_snapshot_source_count)
        self.assertEqual(("NVDA", "AMD", "MU"), prepared.market_watchlist_symbols)
        self.assertIn("Personal: NVDA, AMD", prepared.market_watchlist_block)

    async def test_prepare_prefetches_ticker_memory_without_embedding_call(self) -> None:
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
            prompt="How are NVDA earnings looking?",
            context_text="",
            include_memories=True,
        )

        self.assertEqual(0, memory_service.embedding_calls)
        self.assertEqual([None], memory_service.own_embeddings)

    async def test_prepare_skips_related_embedding_when_memories_are_excluded(self) -> None:
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
            prompt="what should user_id=789 get?",
            context_text="",
            include_memories=False,
            mentioned_user_ids=[789],
        )

        self.assertEqual(0, memory_service.embedding_calls)
        self.assertEqual([], memory_service.related_embeddings)

    async def test_prepare_skips_embedding_for_disabled_related_users(self) -> None:
        memory_service = _DisabledRelatedMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="explain photosynthesis",
            context_text="",
            include_memories=True,
            mentioned_user_ids=[999],
        )

        self.assertEqual([(999,)], memory_service.enabled_user_lookups)
        self.assertEqual(0, memory_service.embedding_calls)
        self.assertEqual([], memory_service.related_embeddings)

    async def test_prepare_reuses_one_hybrid_embedding_for_caller_and_related_users(self) -> None:
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
        self.assertEqual([memory_service.embedding], memory_service.own_embeddings)
        self.assertEqual([memory_service.embedding], memory_service.related_embeddings)

    async def test_prepare_overlaps_embedding_with_batched_context_reads(self) -> None:
        memory_service = _ConcurrentEmbeddingMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )
        timings: dict[str, int] = {}

        prepared = await asyncio.wait_for(
            builder.prepare(
                object(),
                guild_id=123,
                user_id=456,
                prompt="what keyboard should I get for my setup?",
                context_text="",
                include_memories=True,
                timing_metrics=timings,
            ),
            timeout=2,
        )

        self.assertTrue(memory_service.embedding_started.is_set())
        self.assertTrue(memory_service.snapshot_started.is_set())
        self.assertEqual(1, memory_service.embedding_usage_records)
        self.assertEqual([memory_service.embedding], memory_service.own_embeddings)
        self.assertIn("likes keyboards", prepared.personal_profile_block)
        self.assertIn("ctx_settings_ms", timings)
        self.assertIn("ctx_embed_wait_ms", timings)

    async def test_prepare_expands_memory_budget_for_explicit_recall(self) -> None:
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
            prompt="What do you remember about my projects?",
            context_text="",
            include_memories=True,
        )

        self.assertEqual([8], memory_service.own_limits)

    async def test_prepare_uses_hybrid_embedding_when_lexical_memory_matches(self) -> None:
        memory_service = _LexicalHitMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what keyboard should I get for my setup?",
            context_text="",
            include_memories=True,
        )

        self.assertEqual(1, memory_service.embedding_calls)
        self.assertEqual([memory_service.embedding], memory_service.own_embeddings)
        self.assertIn("likes keyboards", prepared.memories_block)

    async def test_prepare_falls_back_to_lexical_when_embedding_is_unavailable(self) -> None:
        memory_service = _UnavailableEmbeddingMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what keyboard should I get for my setup?",
            context_text="",
            include_memories=True,
        )

        self.assertEqual(1, memory_service.embedding_calls)
        self.assertEqual([None], memory_service.own_embeddings)
        self.assertIn("likes keyboards", prepared.memories_block)

    async def test_related_lexical_hit_does_not_block_another_users_semantic_match(self) -> None:
        memory_service = _RelatedHybridMemoryService()
        builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=_FakeChannelAliasService(),
            member_alias_service=_FakeMemberAliasService(),
        )

        prepared = await builder.prepare(
            object(),
            guild_id=123,
            user_id=456,
            prompt="what should I get for my setup with user_id=789 and user_id=790?",
            context_text="",
            include_memories=True,
            mentioned_user_ids=[789, 790],
        )

        self.assertEqual(1, memory_service.embedding_calls)
        self.assertEqual([memory_service.embedding], memory_service.related_embeddings)
        self.assertIn("user_id=789 [fact; preference] user 789 likes keyboards", prepared.mentioned_user_memories_block)
        self.assertIn(
            "user_id=790 [fact; project] user 790 is building an ergonomic setup",
            prepared.mentioned_user_memories_block,
        )


if __name__ == "__main__":
    unittest.main()
