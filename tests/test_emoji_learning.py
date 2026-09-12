from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.config import ConfigurationError, Settings
from nycti.db.models import Base
from nycti.discord.emoji_pool import DAY, maintain_emoji_pool, rotation_victim
from nycti.emoji_catalog import EmojiCatalog, ObservedEmoji
from nycti.emoji_learning import EmojiLearner, EmojiUse
from nycti.emoji_meaning import assess_emoji, safe_meaning, safe_usage_text
from nycti.llm.types import LLMUsage


@dataclass
class FakeEmoji:
    id: int
    name: str
    animated: bool = False
    user: object = field(default_factory=lambda: SimpleNamespace(id=999))
    roles: tuple = ()
    delete: object = field(default_factory=AsyncMock)

    def __str__(self):
        return ObservedEmoji(self.id, self.name, self.animated).token


class EmojiLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.database = SimpleNamespace(session=async_sessionmaker(self.engine, expire_on_commit=False))
        self.catalog = EmojiCatalog(self.database)
        self.guild = SimpleNamespace(
            id=1, emojis=[], emoji_limit=50, default_role=SimpleNamespace(id=1),
            fetch_member=AsyncMock(return_value=SimpleNamespace(guild_permissions=SimpleNamespace(create_expressions=True))),
            fetch_emojis=AsyncMock(return_value=[]), create_custom_emoji=AsyncMock(return_value=FakeEmoji(888, "new")),
        )
        self.settings = Settings(discord_token="fake", openai_api_key="fake", database_url="sqlite:///:memory:",
                                 discord_guild_id=1, openai_vision_model="test-vision")
        self.bot = SimpleNamespace(
            settings=self.settings, emoji_catalog=self.catalog, database=self.database,
            get_guild=Mock(return_value=self.guild), user=SimpleNamespace(id=999), cached_messages=[],
            llm_client=SimpleNamespace(complete_chat=AsyncMock()),
        )
        self.learner = EmojiLearner(self.bot)
        self.bot._emoji_learner = self.learner
        self.now = time.time()
        self.emoji = ObservedEmoji(500, "new")
        await self.catalog.observe(1, self.emoji.token)

    async def asyncTearDown(self):
        await self.learner.close()
        await self.engine.dispose()

    def job(self, i, *, user=None, text=None):
        return EmojiUse(1, 10000+i, user if user is not None else (i % 2 + 1), (self.emoji,),
                        text or f"An amusing moment number {i}", self.now - 300 + i * 20)

    async def qualify(self, source="500"):
        def update(state):
            state.setdefault("usage", {})[source] = {"messages": 5, "score": 10, "last_used": self.now}
            state.setdefault("meanings", {})[source] = {"meaning": "amusement", "qualified": True}
            state.setdefault("image_safety", {})[source] = {"approved": True, "checked_at": self.now}
        await self.catalog.update_learning(1, update)

    async def seed_pool(self, count=20):
        emojis = [FakeEmoji(1000+i, f"old{i}") for i in range(count)]
        self.guild.fetch_emojis.return_value = emojis
        def update(state):
            state["managed"] = {str(100+i): {"id": item.id, "name": item.name, "created_at": self.now-30*DAY}
                                for i, item in enumerate(emojis)}
            state.setdefault("usage", {}).update({str(100+i): {"score": 1, "last_used": self.now-8*DAY}
                                                   for i in range(count)})
        await self.catalog.update_learning(1, update)
        return emojis

    async def test_repeated_context_qualifies_without_llm_for_every_message(self):
        async def assessment_result(*args, **kwargs):
            if kwargs.get("safety_only"):
                return {"approved": True, "confidence": .9}
            return {"meaning": "amused agreement", "confidence": .9, "manual": False}
        assessment = AsyncMock(side_effect=assessment_result)
        with patch("nycti.emoji_learning.assess_emoji", assessment), patch(
            "nycti.emoji_learning.maintain_emoji_pool", new_callable=AsyncMock,
        ) as pool:
            for i in range(5):
                await self.learner.run(self.job(i))
            self.assertEqual(assessment.await_count, 2)
            self.assertTrue(pool.await_count >= 1)
            for i in range(5, 8):
                await self.learner.run(self.job(i))
            self.assertEqual(assessment.await_count, 2)
        state = await self.catalog.learning_state(1)
        self.assertTrue(state["meanings"]["500"]["qualified"])
        self.assertNotIn("amusing moment number", json.dumps(state))
        self.assertNotIn("user_id", json.dumps(state))

    async def test_spam_duplicates_do_not_qualify(self):
        with patch("nycti.emoji_learning.assess_emoji", new_callable=AsyncMock) as assessment:
            for i in range(10):
                await self.learner.run(self.job(0, user=1))
            assessment.assert_not_awaited()
            before = await self.catalog.learning_state(1)
            await self.learner.run(self.job(0, user=1))
            self.assertEqual(before["usage"], (await self.catalog.learning_state(1))["usage"])

    async def test_budget_persists_and_caps_calls_across_restart(self):
        for i in range(4):
            self.assertTrue(await self.learner.reserve_assessment(1, str(i), self.now, manual=True))
        self.bot.emoji_catalog = EmojiCatalog(self.database)
        fresh = EmojiLearner(self.bot)
        self.assertFalse(await fresh.reserve_assessment(1, "last", self.now, manual=True))
        self.assertTrue(await fresh.reserve_assessment(1, "last", self.now+DAY, manual=True))
        await fresh.close()

    async def test_full_pool_rotates_only_one_owned_stale_emoji(self):
        old = await self.seed_pool()
        await self.qualify()
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")):
            await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.assertEqual(sum(item.delete.await_count for item in old), 1)
        state = await self.catalog.learning_state(1)
        self.assertEqual(len(state["managed"]), 20)
        self.assertEqual(state["managed"]["500"]["id"], 888)
        self.assertEqual(state["last_rotation"], self.now)

    async def test_pins_overrides_manual_creator_and_renames_protect_deletion(self):
        old = await self.seed_pool(1)
        self.settings.emoji_auto_import_limit = 1
        await self.qualify()
        await self.catalog.control(1, 100, "pin")
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()
        await self.catalog.control(1, 100, "unpin")
        await self.catalog.override(1, "favorite", ObservedEmoji(1000, "old0"))
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()
        await self.catalog.override(1, "favorite", None)
        old[0].user = SimpleNamespace(id=123)
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()
        old[0].user = SimpleNamespace(id=999)
        old[0].name = "admin_renamed"
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_recently_used_and_rotation_cooldown_protect_pool(self):
        old = await self.seed_pool(1)
        self.settings.emoji_auto_import_limit = 1
        await self.qualify()
        def recent(state):
            state["usage"]["100"]["last_used"] = self.now
        await self.catalog.update_learning(1, recent)
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()
        def cooldown(state):
            state["usage"]["100"]["last_used"] = self.now-8*DAY
            state["last_rotation"] = self.now-3600
        await self.catalog.update_learning(1, cooldown)
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()

    async def test_disabled_pool_blocked_emoji_and_missing_permission_do_not_upload(self):
        await self.qualify()
        self.settings.emoji_auto_import_limit = 0
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.settings.emoji_auto_import_limit = 20
        await self.catalog.control(1, 500, "block")
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        await self.catalog.control(1, 500, "unblock")
        self.guild.fetch_member.return_value.guild_permissions.create_expressions = False
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_ambiguous_upload_reserves_slot_and_is_not_retried_after_restart(self):
        await self.qualify()
        self.guild.create_custom_emoji.side_effect = TimeoutError()
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")):
            with self.assertRaises(TimeoutError):
                await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.bot.emoji_catalog = EmojiCatalog(self.database)
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.guild.create_custom_emoji.assert_awaited_once()
        state = await self.bot.emoji_catalog.learning_state(1)
        self.assertIsNone(state["managed"]["500"]["id"])

    async def test_pin_while_image_downloads_prevents_rotation(self):
        old = await self.seed_pool(1)
        self.settings.emoji_auto_import_limit = 1
        await self.qualify()
        async def download(_):
            await self.catalog.control(1, 100, "pin")
            return b"PNG"
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", side_effect=download):
            await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        old[0].delete.assert_not_awaited()

    async def test_failed_delete_does_not_create_or_release_owned_slot(self):
        old = await self.seed_pool(1)
        self.settings.emoji_auto_import_limit = 1
        await self.qualify()
        old[0].delete.side_effect = TimeoutError()
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")):
            with self.assertRaises(TimeoutError):
                await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.guild.create_custom_emoji.assert_not_awaited()
        self.assertIn("100", (await self.catalog.learning_state(1))["managed"])

    async def test_popular_newcomers_cannot_overfill_twenty_slots(self):
        existing = []
        self.guild.fetch_emojis.return_value = existing
        async def create(**kwargs):
            item = FakeEmoji(2000+len(existing), kwargs["name"])
            existing.append(item)
            return item
        self.guild.create_custom_emoji.side_effect = create
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")):
            for index in range(21):
                await self.qualify(str(500+index))
                await maintain_emoji_pool(self.bot, self.guild, ObservedEmoji(500+index, f"new{index}"), now=self.now)
        self.assertEqual(len(existing), 20)
        self.assertEqual(len((await self.catalog.learning_state(1))["managed"]), 20)

    async def test_schedule_ignores_private_channels_bots_and_disabled_learning(self):
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False, id=1, name="someone", display_name="someone"), guild=self.guild,
            id=1111, content="nice <:new:500>", mentions=[], created_at=datetime.now(timezone.utc),
            channel=SimpleNamespace(id=2, permissions_for=lambda _: SimpleNamespace(view_channel=False)),
        )
        self.assertFalse(self.learner.schedule(message))
        message.channel.permissions_for = lambda _: SimpleNamespace(view_channel=True)
        with patch.object(self.learner.jobs, "submit", return_value=True) as submit:
            self.assertTrue(self.learner.schedule(message))
            submit.assert_called_once()
            self.settings.emoji_learning_enabled = False
            self.assertFalse(self.learner.schedule(message))

    async def test_inference_payload_uses_image_and_context_and_accounts_usage(self):
        usage = LLMUsage(feature="emoji_learn", model="test-vision", prompt_tokens=10,
                         completion_tokens=5, total_tokens=15, estimated_cost_usd=0)
        self.bot.llm_client.complete_chat.return_value = SimpleNamespace(
            text=json.dumps({"safe": True, "confidence": .9, "meaning": "playful disagreement"}), usage=usage,
        )
        with patch("nycti.emoji_meaning.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")), patch(
            "nycti.emoji_meaning.record_usage", new_callable=AsyncMock,
        ) as record:
            result = await assess_emoji(self.bot, guild_id=1, emoji=self.emoji,
                                        examples=("sure buddy", "not buying it", "very convincing"))
        self.assertEqual(result["meaning"], "playful disagreement")
        request = self.bot.llm_client.complete_chat.call_args.kwargs
        self.assertIn("sure buddy", request["messages"][1]["content"][0]["text"])
        self.assertTrue(request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png"))
        record.assert_awaited_once()

    async def test_low_confidence_and_sensitive_model_output_are_rejected(self):
        for safe, confidence, meaning in [(True, .3, "sarcasm"), (False, .99, "agreement"),
                                          (True, .99, "account balance is empty")]:
            self.bot.llm_client.complete_chat.return_value = SimpleNamespace(
                text=json.dumps(dict(safe=safe, confidence=confidence, meaning=meaning)), usage=object(),
            )
            with patch("nycti.emoji_meaning.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")), patch(
                "nycti.emoji_meaning.record_usage", new_callable=AsyncMock,
            ):
                self.assertIsNone(await assess_emoji(self.bot, guild_id=1, emoji=self.emoji, examples=("test",)))

    async def test_manual_meaning_survives_automatic_learning_and_forget_stops_relearning(self):
        self.settings.emoji_auto_import_limit = 0
        with patch("nycti.emoji_learning.assess_emoji", new=AsyncMock(return_value={
            "meaning": "agreement", "confidence": .99, "manual": True,
        })):
            await self.learner.set_meaning(self.guild, self.emoji, "agreement")
        with patch("nycti.emoji_learning.assess_emoji", new_callable=AsyncMock) as assess:
            for i in range(5):
                await self.learner.run(self.job(i))
            assess.assert_not_awaited()
            await self.catalog.control(1, 500, "forget_meaning")
            await self.learner.run(self.job(6))
            assess.assert_not_awaited()
        self.assertNotIn("500", (await self.catalog.learning_state(1))["meanings"])

    async def test_two_uses_one_author_imports_without_meaning_or_context(self):
        with patch("nycti.emoji_learning.assess_emoji", new=AsyncMock(return_value={"approved": True})) as assess, patch(
            "nycti.emoji_learning.maintain_emoji_pool", new_callable=AsyncMock,
        ) as pool:
            await self.learner.run(self.job(0, user=1, text="[emoji]"))
            assess.assert_not_awaited()
            await self.learner.run(self.job(1, user=1, text="[emoji]"))
            assess.assert_awaited_once()
            self.assertTrue(assess.call_args.kwargs["safety_only"])
            self.assertEqual(assess.call_args.kwargs["examples"], ())
            pool.assert_awaited_once()
        state = await self.catalog.learning_state(1)
        self.assertTrue(state["image_safety"]["500"]["approved"])
        self.assertNotIn("500", state.get("meanings", {}))

    async def test_restart_retains_progress_for_two_use_import(self):
        await self.learner.run(self.job(0, user=1, text="[emoji]"))
        await self.learner.close()
        self.bot.emoji_catalog = EmojiCatalog(self.database)
        self.learner = EmojiLearner(self.bot)
        with patch("nycti.emoji_learning.assess_emoji", new=AsyncMock(return_value={"approved": True})), patch(
            "nycti.emoji_learning.maintain_emoji_pool", new_callable=AsyncMock,
        ) as pool:
            await self.learner.run(self.job(1, user=1, text="[emoji]"))
            pool.assert_awaited_once()

    async def test_reactions_count_on_old_messages_but_toggling_does_not(self):
        await self.learner.run(self.job(0, user=1, text="[emoji]"))
        reaction = EmojiUse(1, 5, 1, (self.emoji,), "", self.now, kind="reaction")
        with patch("nycti.emoji_learning.assess_emoji", new=AsyncMock(return_value={"approved": True})), patch(
            "nycti.emoji_learning.maintain_emoji_pool", new_callable=AsyncMock,
        ) as pool:
            await self.learner.run(reaction)
            pool.assert_awaited_once()
            await self.learner.run(reaction)
        usage = (await self.catalog.learning_state(1))["usage"]["500"]
        self.assertEqual(usage["uses"], 2)
        self.assertEqual(usage["messages"], 1)
        self.assertEqual(usage["reactions"], 1)

    async def test_raw_reactions_include_uncached_messages_and_skip_private_or_bot(self):
        import discord
        channel = SimpleNamespace(permissions_for=lambda _: SimpleNamespace(view_channel=True), is_private=lambda: False)
        self.guild.get_channel_or_thread = lambda _: channel
        member = SimpleNamespace(bot=False)
        payload = SimpleNamespace(guild_id=1, channel_id=2, message_id=5, user_id=3, member=member,
                                  emoji=discord.PartialEmoji(name="new", id=500))
        with patch.object(self.learner.jobs, "submit", return_value=True) as submit:
            self.assertTrue(await self.learner.schedule_reaction(payload))
            self.assertEqual(submit.call_args.args[0].kind, "reaction")
            self.assertEqual(submit.call_args.args[0].context, "")
            member.bot = True
            self.assertFalse(await self.learner.schedule_reaction(payload))
            member.bot = False
            channel.is_private = lambda: True
            self.assertFalse(await self.learner.schedule_reaction(payload))
            submit.assert_called_once()

    async def test_image_safety_can_pass_without_inventing_meaning(self):
        self.bot.llm_client.complete_chat.return_value = SimpleNamespace(
            text=json.dumps({"safe": True, "confidence": .95}), usage=object(),
        )
        with patch("nycti.emoji_meaning.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")), patch(
            "nycti.emoji_meaning.record_usage", new_callable=AsyncMock,
        ):
            result = await assess_emoji(self.bot, guild_id=1, emoji=self.emoji, examples=(), safety_only=True)
            self.assertTrue(result["approved"])
            self.assertNotIn("meaning", result)
            self.assertIsNone(await assess_emoji(self.bot, guild_id=1, emoji=self.emoji, examples=()))

    async def test_meaning_does_not_bypass_rejected_safety(self):
        await self.qualify()
        await self.catalog.update_learning(1, lambda state: state["image_safety"].clear())
        await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_forget_meaning_does_not_block_safe_import(self):
        await self.qualify()
        await self.catalog.control(1, 500, "forget_meaning")
        with patch("nycti.discord.emoji_pool.fetch_emoji_image", new=AsyncMock(return_value=b"PNG")):
            await maintain_emoji_pool(self.bot, self.guild, self.emoji, now=self.now)
        self.guild.create_custom_emoji.assert_awaited_once()

    async def test_inferred_meaning_is_labeled_and_blocked_emoji_not_exposed(self):
        await self.qualify()
        self.guild.emojis = [FakeEmoji(500, "new")]
        self.assertIn("tentative", self.catalog.prompt(self.guild, "new"))
        await self.catalog.control(1, 500, "block")
        self.assertNotIn(":new:", self.catalog.prompt(self.guild, "new"))


class EmojiLearningSafetyTests(unittest.TestCase):
    def test_input_filters_and_configuration_limits(self):
        self.assertEqual("", safe_usage_text("my api key is sk-secretwhatever"))
        self.assertFalse(safe_meaning("used to reveal <@123456>"))
        self.assertFalse(safe_meaning("salary jokes"))
        for limit in (-1, 21):
            with self.assertRaises(ConfigurationError):
                Settings(discord_token="fake", openai_api_key="fake", database_url="sqlite:///:memory:", emoji_auto_import_limit=limit)
        settings = Settings.from_env({"DISCORD_TOKEN": "fake", "OPENAI_API_KEY": "fake", "DATABASE_URL": "sqlite:///:memory:",
                                      "EMOJI_AUTO_IMPORT_LIMIT": "0", "EMOJI_LEARNING_ENABLED": "false"})
        self.assertEqual(settings.emoji_auto_import_limit, 0)
        self.assertFalse(settings.emoji_learning_enabled)
