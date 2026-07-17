import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import nycti.message_context as message_context_module
from nycti.message_context import (
    MessageContextCollector,
    clean_trigger_content,
    collect_message_members,
    dedupe_image_refs,
    dedupe_lines,
    expand_user_mentions,
    format_message_line,
    image_refs_for_message,
    message_has_visible_content,
)


class _FakeHistoryChannel:
    def __init__(
        self,
        messages: list[object],
        *,
        resolved_messages: dict[int, object] | None = None,
        channel_id: int | None = None,
        guild: object | None = None,
        history_error: Exception | None = None,
        view_allowed: bool = True,
    ) -> None:
        self.messages = messages
        self.resolved_messages = resolved_messages or {}
        self.id = channel_id
        self.guild = guild
        self.history_error = history_error
        self.view_allowed = view_allowed
        self.history_calls = 0
        self.fetch_message_calls = 0
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
        self.history_calls += 1
        if self.history_error is not None:
            raise self.history_error
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
        self.fetch_message_calls += 1
        if message_id in self.resolved_messages:
            return self.resolved_messages[message_id]
        for message in self.messages:
            if getattr(message, "id", None) == message_id:
                return message
        raise AssertionError(f"Message {message_id} not found in fake channel")

    def permissions_for(self, _member: object) -> object:
        return SimpleNamespace(
            view_channel=self.view_allowed,
            read_messages=self.view_allowed,
        )


class _FakeBot:
    def __init__(
        self,
        *,
        cached_messages: list[object] | None = None,
        channels: dict[int, object] | None = None,
    ) -> None:
        self.cached_messages = cached_messages or []
        self.channels = channels or {}
        self.get_message_calls: list[int] = []
        self.get_channel_calls: list[int] = []
        self.fetch_channel_calls: list[int] = []

    def get_message(self, message_id: int):  # type: ignore[no-untyped-def]
        self.get_message_calls.append(message_id)
        return next(
            (item for item in self.cached_messages if getattr(item, "id", None) == message_id),
            None,
        )

    def get_channel(self, channel_id: int):  # type: ignore[no-untyped-def]
        self.get_channel_calls.append(channel_id)
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):  # type: ignore[no-untyped-def]
        self.fetch_channel_calls.append(channel_id)
        return self.channels.get(channel_id)


class MessageContextHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_collect_message_members_dedupes_authors_and_mentions(self) -> None:
        lucis = SimpleNamespace(id=123, display_name="Lucis")
        mat = SimpleNamespace(id=456, display_name="mat")
        messages = [
            SimpleNamespace(author=lucis, mentions=[mat]),
            SimpleNamespace(author=mat, mentions=[lucis]),
        ]

        self.assertEqual([lucis, mat], collect_message_members(messages))

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

    def test_clean_trigger_content_preserves_name_when_it_is_the_subject(self) -> None:
        message = SimpleNamespace(content="What does Nycti mean?", mentions=[])

        self.assertEqual(
            clean_trigger_content(
                message,
                bot_user_id=None,
                strip_invocation_name=False,
            ),
            "What does Nycti mean?",
        )

    def test_clean_trigger_content_only_removes_leading_named_invocation(self) -> None:
        message = SimpleNamespace(
            content="Hey Nycti, what does Nycti mean?",
            mentions=[],
        )

        self.assertEqual(
            clean_trigger_content(message, bot_user_id=None),
            "what does Nycti mean?",
        )

    def test_clean_trigger_content_handles_punctuated_bot_mentions(self) -> None:
        message = SimpleNamespace(content="<@123>, what does Nycti mean?", mentions=[])

        self.assertEqual(
            clean_trigger_content(
                message,
                bot_user_id=123,
                strip_invocation_name=False,
            ),
            "what does Nycti mean?",
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

    def test_message_has_visible_content_accepts_attachment_only_messages(self) -> None:
        message = SimpleNamespace(content="   ", attachments=[SimpleNamespace()])
        self.assertTrue(message_has_visible_content(message))

    def test_message_has_visible_content_accepts_embed_only_messages(self) -> None:
        message = SimpleNamespace(
            content="   ",
            attachments=[],
            embeds=[SimpleNamespace(title="NVDA Earnings", description="Beat and raise")],
        )
        self.assertTrue(message_has_visible_content(message))

    def test_format_message_line_uses_attachment_placeholder_when_text_is_empty(self) -> None:
        message = SimpleNamespace(
            content="",
            attachments=[SimpleNamespace(), SimpleNamespace()],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(format_message_line(message), "mat: [2 attachment(s)]")

    def test_format_message_line_uses_embed_preview_when_text_is_empty(self) -> None:
        message = SimpleNamespace(
            content="",
            attachments=[],
            embeds=[
                SimpleNamespace(
                    title="SPX Update",
                    description="Index closes higher",
                    provider=SimpleNamespace(name="Bloomberg"),
                )
            ],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(
            format_message_line(message),
            "mat: [embed: Bloomberg: SPX Update — Index closes higher]",
        )

    def test_format_message_line_appends_embed_preview_to_text(self) -> None:
        message = SimpleNamespace(
            content="check this link",
            attachments=[],
            embeds=[SimpleNamespace(title="Cartier Tank", description="Product page")],
            author=SimpleNamespace(display_name="mat"),
        )
        self.assertEqual(
            format_message_line(message),
            "mat: check this link [embed: Cartier Tank — Product page]",
        )

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

    async def test_build_message_context_omits_recent_timestamps_and_skips_older_than_24h(self) -> None:
        current_time = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        old_message = SimpleNamespace(
            id=1,
            content="old context",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="old"),
            created_at=datetime(2026, 4, 10, 19, 59, tzinfo=timezone.utc),
        )
        recent_message = SimpleNamespace(
            id=2,
            content="recent context",
            attachments=[],
            mentions=[],
            author=SimpleNamespace(display_name="recent"),
            created_at=datetime(2026, 4, 12, 19, 30, tzinfo=timezone.utc),
        )
        current_message = SimpleNamespace(
            id=3,
            content="<@123> current ask",
            attachments=[],
            mentions=[SimpleNamespace(id=123, display_name="Nycti")],
            author=SimpleNamespace(display_name="mat"),
            created_at=current_time,
            reference=None,
            guild=None,
        )
        current_message.channel = _FakeHistoryChannel([old_message, recent_message])
        collector = MessageContextCollector(
            bot=SimpleNamespace(),
            channel_context_limit=5,
            max_reply_chain_depth=0,
            max_linked_message_count=0,
            max_context_image_count=0,
            anchor_context_per_side=0,
        )

        context_lines, _, _ = await collector.build_message_context(current_message)

        self.assertIn("recent: recent context", context_lines)
        self.assertNotIn("old context", "\n".join(context_lines))
        self.assertFalse(any("2026-04-12" in line for line in context_lines))

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

    async def test_recent_context_uses_full_same_channel_cache_without_history_request(self) -> None:
        guild = SimpleNamespace(id=1)
        other_guild = SimpleNamespace(id=2)
        now = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        channel = _FakeHistoryChannel([], channel_id=10, guild=guild)
        other_channel = _FakeHistoryChannel([], channel_id=11, guild=guild)
        wrong_guild_channel = _FakeHistoryChannel([], channel_id=10, guild=other_guild)
        cached = [
            _message(3, "third", now, channel),
            _message(1, "first", now, channel),
            _message(2, "second", now, channel),
            _message(4, "too old", datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc), channel),
            _message(5, "other channel", now, other_channel),
            _message(6, "wrong guild", now, wrong_guild_channel),
            _message(101, "after current", now, channel),
        ]
        current = _message(100, "current", now, channel, guild=guild)
        collector = _collector(_FakeBot(cached_messages=cached), context_limit=2)

        messages = await collector._fetch_context_messages(channel, before=current)

        self.assertEqual([2, 3], [item.id for item in messages])
        self.assertEqual(0, channel.history_calls)

    async def test_recent_context_fetches_and_merges_when_cache_is_partial(self) -> None:
        guild = SimpleNamespace(id=1)
        now = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        history_messages: list[object] = []
        channel = _FakeHistoryChannel(history_messages, channel_id=10, guild=guild)
        history_messages.extend([
            _message(1, "first", now, channel, guild=guild),
            _message(2, "second", now, channel, guild=guild),
            _message(3, "third", now, channel, guild=guild),
            _message(4, "fourth", now, channel, guild=guild),
        ])
        cached = [_message(5, "fifth", now, channel, guild=guild)]
        current = _message(100, "current", now, channel, guild=guild)
        collector = _collector(_FakeBot(cached_messages=cached), context_limit=5)

        messages = await collector._fetch_context_messages(channel, before=current)

        self.assertEqual([1, 2, 3, 4, 5], [item.id for item in messages])
        self.assertEqual(1, channel.history_calls)

    async def test_recent_context_deduplicates_cache_overlap_with_history(self) -> None:
        guild = SimpleNamespace(id=1)
        now = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        history_messages: list[object] = []
        channel = _FakeHistoryChannel(history_messages, channel_id=10, guild=guild)
        history_messages.extend([
            _message(message_id, f"history {message_id}", now, channel, guild=guild)
            for message_id in range(1, 6)
        ])
        cached = [
            _message(4, "cached fourth", now, channel, guild=guild),
            _message(5, "cached fifth", now, channel, guild=guild),
        ]
        current = _message(100, "current", now, channel, guild=guild)
        collector = _collector(_FakeBot(cached_messages=cached), context_limit=5)

        messages = await collector._fetch_context_messages(channel, before=current)

        self.assertEqual([1, 2, 3, 4, 5], [item.id for item in messages])
        self.assertEqual(["cached fourth", "cached fifth"], [item.content for item in messages[-2:]])
        self.assertEqual(1, channel.history_calls)

    async def test_recent_context_keeps_partial_cache_when_history_fetch_fails(self) -> None:
        guild = SimpleNamespace(id=1)
        now = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        channel = _FakeHistoryChannel(
            [],
            channel_id=10,
            guild=guild,
            history_error=message_context_module.discord.HTTPException(
                SimpleNamespace(status=503, reason="Unavailable", headers={}),
                "history unavailable",
            ),
        )
        cached = [_message(5, "cached fifth", now, channel, guild=guild)]
        current = _message(100, "current", now, channel, guild=guild)
        collector = _collector(_FakeBot(cached_messages=cached), context_limit=5)

        messages = await collector._fetch_context_messages(channel, before=current)

        self.assertEqual([5], [item.id for item in messages])
        self.assertEqual(1, channel.history_calls)

    async def test_recent_context_falls_back_to_history_when_cache_has_no_useful_message(self) -> None:
        guild = SimpleNamespace(id=1)
        now = datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc)
        history_messages: list[object] = []
        channel = _FakeHistoryChannel(history_messages, channel_id=10, guild=guild)
        history_messages.extend([
            _message(1, "first", now, channel),
            _message(2, "second", now, channel),
        ])
        other_channel = _FakeHistoryChannel([], channel_id=11, guild=guild)
        cached = [_message(3, "not this channel", now, other_channel)]
        current = _message(100, "current", now, channel, guild=guild)
        collector = _collector(_FakeBot(cached_messages=cached), context_limit=5)

        messages = await collector._fetch_context_messages(channel, before=current)

        self.assertEqual([1, 2], [item.id for item in messages])
        self.assertEqual(1, channel.history_calls)

    async def test_reply_resolution_uses_client_message_cache_before_fetch(self) -> None:
        guild = SimpleNamespace(id=1)
        channel = _FakeHistoryChannel([], channel_id=10, guild=guild)
        referenced = _message(40, "cached reply", datetime.now(timezone.utc), channel, guild=guild)
        bot = _FakeBot(cached_messages=[referenced])
        collector = _collector(bot)
        current = _message(50, "reply", datetime.now(timezone.utc), channel, guild=guild)
        current.reference = SimpleNamespace(message_id=40, resolved=None, cached_message=None)

        resolved = await collector._resolve_referenced_message(current)

        self.assertIs(referenced, resolved)
        self.assertEqual([40], bot.get_message_calls)
        self.assertEqual(0, channel.fetch_message_calls)

    async def test_reply_resolution_fetches_message_on_cache_miss(self) -> None:
        guild = SimpleNamespace(id=1)
        referenced = _message(40, "fetched reply", datetime.now(timezone.utc), None, guild=guild)
        channel = _FakeHistoryChannel(
            [],
            resolved_messages={40: referenced},
            channel_id=10,
            guild=guild,
        )
        referenced.channel = channel
        collector = _collector(_FakeBot())
        current = _message(50, "reply", datetime.now(timezone.utc), channel, guild=guild)
        current.reference = SimpleNamespace(message_id=40, resolved=None, cached_message=None)

        resolved = await collector._resolve_referenced_message(current)

        self.assertIs(referenced, resolved)
        self.assertEqual(1, channel.fetch_message_calls)

    async def test_link_resolution_checks_channel_acl_before_using_message_cache(self) -> None:
        guild = SimpleNamespace(id=1)
        fallback = _FakeHistoryChannel([], channel_id=10, guild=guild)
        linked_channel = _FakeHistoryChannel([], channel_id=20, guild=guild)
        linked = _message(80, "cached link", datetime.now(timezone.utc), linked_channel, guild=guild)
        bot = _FakeBot(cached_messages=[linked], channels={20: linked_channel})
        collector = _collector(bot)

        resolved = await collector._fetch_linked_message(
            guild=guild,
            fallback_channel=fallback,
            requester=SimpleNamespace(id=7),
            channel_id=20,
            message_id=80,
        )

        self.assertIs(linked, resolved)
        self.assertEqual([20], bot.get_channel_calls)
        self.assertEqual(0, linked_channel.fetch_message_calls)

    async def test_link_resolution_fetches_message_on_cache_miss(self) -> None:
        guild = SimpleNamespace(id=1)
        fallback = _FakeHistoryChannel([], channel_id=10, guild=guild)
        linked = _message(80, "fetched link", datetime.now(timezone.utc), None, guild=guild)
        linked_channel = _FakeHistoryChannel(
            [],
            resolved_messages={80: linked},
            channel_id=20,
            guild=guild,
        )
        linked.channel = linked_channel
        bot = _FakeBot(channels={20: linked_channel})
        collector = _collector(bot)

        resolved = await collector._fetch_linked_message(
            guild=guild,
            fallback_channel=fallback,
            requester=SimpleNamespace(id=7),
            channel_id=20,
            message_id=80,
        )

        self.assertIs(linked, resolved)
        self.assertEqual([20], bot.get_channel_calls)
        self.assertEqual(1, linked_channel.fetch_message_calls)

    async def test_link_resolution_denies_requester_without_target_channel_access(self) -> None:
        guild = SimpleNamespace(id=1)
        fallback = _FakeHistoryChannel([], channel_id=10, guild=guild)
        linked_channel = _FakeHistoryChannel(
            [],
            channel_id=20,
            guild=guild,
            view_allowed=False,
        )
        linked = _message(80, "private cached link", datetime.now(timezone.utc), linked_channel, guild=guild)
        bot = _FakeBot(cached_messages=[linked], channels={20: linked_channel})
        collector = _collector(bot)

        resolved = await collector._fetch_linked_message(
            guild=guild,
            fallback_channel=fallback,
            requester=SimpleNamespace(id=7),
            channel_id=20,
            message_id=80,
        )

        self.assertIsNone(resolved)
        self.assertEqual([20], bot.get_channel_calls)
        self.assertEqual(0, linked_channel.fetch_message_calls)


def _collector(bot: object, *, context_limit: int = 5) -> MessageContextCollector:
    return MessageContextCollector(
        bot=bot,
        channel_context_limit=context_limit,
        max_reply_chain_depth=2,
        max_linked_message_count=2,
        max_context_image_count=0,
        anchor_context_per_side=0,
    )


def _message(
    message_id: int,
    content: str,
    created_at: datetime,
    channel: object | None,
    *,
    guild: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content=content,
        attachments=[],
        embeds=[],
        mentions=[],
        author=SimpleNamespace(display_name="user"),
        created_at=created_at,
        channel=channel,
        guild=guild,
        reference=None,
    )


if __name__ == "__main__":
    unittest.main()
