from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.db.models import Base
from nycti.emoji_backfill import EmojiHistory, EmojiHistoryBackfill, HistoricalEmoji, collect_emoji_history, merge_emoji_history
from nycti.emoji_catalog import EmojiCatalog, ObservedEmoji
from nycti.emoji_learning import EmojiLearner, EmojiUse


class Channel:
    def __init__(self, guild, messages, *, public=True, readable=True):
        self.id = 1
        self.guild = guild
        self.messages = messages
        self.public = public
        self.readable = readable
        self.calls = []

    def permissions_for(self, principal):
        return SimpleNamespace(view_channel=self.public, read_message_history=self.readable)

    async def history(self, **kwargs):
        self.calls.append(kwargs)
        for message in self.messages[:kwargs["limit"]]:
            yield message


class Reaction:
    def __init__(self, emoji, users):
        self.emoji = emoji
        self.people = users
        self.calls = 0

    async def users(self, *, limit):
        self.calls += 1
        for user in self.people[:limit]:
            yield user


class EmojiBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.catalog = EmojiCatalog(SimpleNamespace(session=async_sessionmaker(self.engine, expire_on_commit=False)))
        self.now = datetime.now(timezone.utc)
        self.human = SimpleNamespace(id=4, bot=False)
        self.guild = SimpleNamespace(id=1, default_role=object(), fetch_member=AsyncMock(return_value=object()), emojis=[])
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=9), emoji_catalog=self.catalog, get_guild=lambda _: self.guild,
            settings=SimpleNamespace(emoji_learning_enabled=True, openai_vision_model="fake", discord_guild_id=1,
                                     emoji_auto_import_limit=20),
            _emoji_learner=SimpleNamespace(try_import=AsyncMock()),
        )
        self.backfill = EmojiHistoryBackfill(self.bot)
        self.emoji = ObservedEmoji(500, "test")

    async def asyncTearDown(self):
        await self.backfill.close()
        await self.engine.dispose()

    def message(self, mid, *, days=1, bot=False, content=None, reactions=()):
        return SimpleNamespace(id=mid, author=SimpleNamespace(bot=bot, id=4),
                               created_at=self.now-timedelta(days=days),
                               content=content or self.emoji.token, reactions=reactions)

    async def test_scan_counts_recent_human_messages_and_reactions_only(self):
        reaction = Reaction(self.emoji.token, [self.human, SimpleNamespace(id=9, bot=True)])
        channel = Channel(self.guild, [self.message(1), self.message(2, bot=True, reactions=[reaction]),
                                       self.message(3, days=8), self.message(4, content=self.emoji.token*3)])
        self.guild.text_channels = [channel]
        history = await collect_emoji_history(self.bot, self.guild, now=self.now)
        self.assertEqual(history.messages, 3)
        self.assertEqual(len(history.emojis[500].uses), 3)
        self.assertEqual(reaction.calls, 1)
        self.assertEqual(channel.calls[0]["after"], self.now-timedelta(days=7))

    async def test_private_unreadable_channels_and_other_guild_are_never_read(self):
        channels = [Channel(self.guild, [], public=False), Channel(self.guild, [], readable=False),
                    Channel(SimpleNamespace(id=2), [])]
        self.guild.text_channels = channels
        history = await collect_emoji_history(self.bot, self.guild, now=self.now)
        self.assertEqual(history.channels, 0)
        self.assertTrue(all(not channel.calls for channel in channels))

    async def test_channel_message_and_reaction_limits(self):
        reactions = [Reaction(self.emoji.token, [self.human]) for _ in range(4)]
        self.guild.text_channels = [Channel(self.guild, [self.message(i, reactions=reactions) for i in range(4)]) for _ in range(3)]
        with patch("nycti.emoji_backfill.MAX_CHANNELS", 1), patch("nycti.emoji_backfill.MAX_MESSAGES_PER_CHANNEL", 2), patch("nycti.emoji_backfill.MAX_REACTION_LOOKUPS", 1):
            history = await collect_emoji_history(self.bot, self.guild, now=self.now)
        self.assertEqual(history.channels, 1)
        self.assertEqual(history.messages, 2)
        self.assertEqual(history.reaction_lookups, 1)
        self.assertTrue(history.capped)

    def sample(self):
        entry = HistoricalEmoji(self.emoji)
        for mid in (10, 20):
            entry.add(kind="message", message_id=mid, user_id=4, timestamp=(self.now-timedelta(days=2)).timestamp())
        entry.add(kind="reaction", message_id=10, user_id=4, timestamp=(self.now-timedelta(days=2)).timestamp())
        return EmojiHistory(emojis={500: entry}, channels=1, messages=2)

    async def test_merge_is_idempotent_and_preserves_newer_live_counts(self):
        history = self.sample()
        await self.catalog.update_learning(1, lambda state: state.update(usage={"500": {
            "messages": 1, "uses": 1, "score": 1.0, "last_message": 20,
            "last_used": (self.now-timedelta(days=2)).timestamp(), "safety_attempt_at": 15,
        }}))
        await merge_emoji_history(self.catalog, 1, history, now=self.now.timestamp())
        first = await self.catalog.learning_state(1)
        await merge_emoji_history(self.catalog, 1, history, now=self.now.timestamp()+100)
        self.assertEqual(first, await self.catalog.learning_state(1))
        self.assertEqual(first["usage"]["500"]["uses"], 3)
        self.assertEqual(first["usage"]["500"]["safety_attempt_at"], 15)
        self.assertNotIn("content", json.dumps(first))
        await self.catalog.update_learning(1, lambda state: state["usage"]["500"].update(messages=10, uses=11, last_message=99))
        await merge_emoji_history(self.catalog, 1, history, now=self.now.timestamp())
        self.assertEqual((await self.catalog.learning_state(1))["usage"]["500"]["uses"], 11)

    async def test_replayed_historical_reaction_is_not_counted_after_restart(self):
        await merge_emoji_history(self.catalog, 1, self.sample(), now=self.now.timestamp())
        learner = EmojiLearner(self.bot)
        try:
            await learner.run(EmojiUse(1, 10, 4, (self.emoji,), "", self.now.timestamp(), kind="reaction"))
            self.assertEqual((await self.catalog.learning_state(1))["usage"]["500"]["uses"], 3)
            self.bot._emoji_learner.try_import.assert_not_awaited()
        finally:
            await learner.close()

    async def test_backfill_hands_counts_to_existing_import_checks_once(self):
        self.guild.text_channels = [Channel(self.guild, [self.message(20), self.message(10)])]
        result = await self.backfill.run(self.guild)
        self.assertIn("Scanned 2", result)
        self.bot._emoji_learner.try_import.assert_awaited_once()
        state = self.bot._emoji_learner.try_import.call_args.args[3]
        self.assertEqual(state["usage"]["500"]["uses"], 2)
        fresh = EmojiHistoryBackfill(self.bot)
        self.assertIn("already complete", await fresh.run(self.guild))
        self.assertEqual(len(self.guild.text_channels[0].calls), 1)

    async def test_failures_do_not_mark_completed_and_force_obeys_cooldown(self):
        with patch("nycti.emoji_backfill.collect_emoji_history", new=AsyncMock(side_effect=TimeoutError())):
            self.assertIn("failed", await self.backfill.run(self.guild))
        self.assertEqual((await self.catalog.learning_state(1))["backfill"]["status"], "failed")
        self.assertIn("once per hour", await self.backfill.run(self.guild, force=True))

    async def test_disabled_backfill_does_not_read_history(self):
        self.bot.settings.emoji_auto_import_limit = 0
        self.assertIn("disabled", await self.backfill.run(self.guild))
        self.guild.fetch_member.assert_not_awaited()
