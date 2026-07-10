import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nycti.discord.invocation import (
    AMBIENT_ADDRESSEDNESS_FEATURE,
    AmbientAddressednessClassifier,
    AmbientInvocationCooldown,
    DiscordInvocationPolicy,
    InvocationReason,
    has_explicit_name_prefix,
    strip_explicit_name_prefix,
)


class InvocationHelpersTests(unittest.TestCase):
    def test_explicit_name_requires_a_leading_direct_address(self) -> None:
        self.assertTrue(
            has_explicit_name_prefix("Hey Nycti, can you help?", invocation_name="Nycti")
        )
        self.assertTrue(
            has_explicit_name_prefix("nycti: can you help?", invocation_name="Nycti")
        )
        self.assertFalse(
            has_explicit_name_prefix("What does Nycti mean?", invocation_name="Nycti")
        )
        self.assertFalse(
            has_explicit_name_prefix("Nycticorax is a genus", invocation_name="Nycti")
        )

    def test_explicit_name_cleanup_only_removes_the_address(self) -> None:
        self.assertEqual(
            strip_explicit_name_prefix(
                "Hey Nycti, what does Nycti mean?",
                invocation_name="Nycti",
            ),
            "what does Nycti mean?",
        )

    def test_ambient_cooldown_is_scoped_to_user_and_channel(self) -> None:
        cooldown = AmbientInvocationCooldown(30)

        self.assertTrue(cooldown.allow(channel_id=10, user_id=20, now=100))
        self.assertFalse(cooldown.allow(channel_id=10, user_id=20, now=129))
        self.assertTrue(cooldown.allow(channel_id=10, user_id=21, now=129))
        self.assertTrue(cooldown.allow(channel_id=11, user_id=20, now=129))
        self.assertTrue(cooldown.allow(channel_id=10, user_id=20, now=130))
        self.assertFalse(cooldown.ready(channel_id=10, user_id=20, now=159))
        self.assertTrue(cooldown.ready(channel_id=10, user_id=20, now=160))


class AmbientAddressednessClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_bounded_economy_call_and_strict_boolean_json(self) -> None:
        llm = SimpleNamespace(
            is_model_available=lambda _model: True,
            complete_chat=AsyncMock(
                return_value=SimpleNamespace(text='{"addressed":true}')
            ),
        )
        classifier = AmbientAddressednessClassifier(
            llm_client=llm,
            model="economy-model",
        )

        self.assertTrue(await classifier.is_addressed("¿Puedes ayudar con esto?"))

        kwargs = llm.complete_chat.await_args.kwargs
        self.assertEqual("economy-model", kwargs["model"])
        self.assertEqual(AMBIENT_ADDRESSEDNESS_FEATURE, kwargs["feature"])
        self.assertEqual(24, kwargs["max_tokens"])
        self.assertEqual(0, kwargs["request_max_retries"])
        encoded_message = kwargs["messages"][1]["content"]
        self.assertEqual(
            {"message": "¿Puedes ayudar con esto?"},
            json.loads(encoded_message),
        )

    async def test_malformed_extra_key_oversized_and_failure_all_fail_closed(self) -> None:
        llm = SimpleNamespace(
            is_model_available=lambda _model: True,
            complete_chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(text="yes"),
                    SimpleNamespace(text='{"addressed":true,"reason":"injected"}'),
                    RuntimeError("provider down"),
                ]
            ),
        )
        classifier = AmbientAddressednessClassifier(
            llm_client=llm,
            model="economy-model",
            max_content_chars=20,
        )

        self.assertFalse(await classifier.is_addressed("question one"))
        self.assertFalse(await classifier.is_addressed("question two"))
        self.assertFalse(await classifier.is_addressed("x" * 21))
        self.assertFalse(await classifier.is_addressed("question three"))
        self.assertEqual(3, llm.complete_chat.await_count)

    async def test_timeout_fails_closed(self) -> None:
        async def slow_call(**_kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.1)
            return SimpleNamespace(text='{"addressed":true}')

        classifier = AmbientAddressednessClassifier(
            llm_client=SimpleNamespace(
                is_model_available=lambda _model: True,
                complete_chat=slow_call,
            ),
            model="economy-model",
            timeout_seconds=0.01,
        )

        self.assertFalse(await classifier.is_addressed("please help"))


class BotInvocationPolicyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _bot(
        *,
        modes: tuple[str, ...],
        ambient_channel_ids: tuple[int, ...] = (),
        guild_id: int = 1,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(id=99),
            invocation_policy=DiscordInvocationPolicy(
                modes=frozenset(modes),
                invocation_name="Nycti",
                ambient_channel_ids=frozenset(ambient_channel_ids),
                ambient_cooldown_seconds=30,
                configured_guild_id=guild_id,
                ambient_classifier=SimpleNamespace(
                    is_addressed=AsyncMock(
                        side_effect=lambda content: content.startswith("What")
                    )
                ),
            ),
        )

    @staticmethod
    def _message(
        content: str,
        *,
        channel_id: int = 10,
        user_id: int = 20,
        guild_id: int | None = 1,
        author_is_bot: bool = False,
        reference: object | None = None,
        mentions: list[object] | None = None,
        role_mentions: list[object] | None = None,
        mention_everyone: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            content=content,
            author=SimpleNamespace(id=user_id, bot=author_is_bot),
            guild=None if guild_id is None else SimpleNamespace(id=guild_id),
            channel=SimpleNamespace(id=channel_id, fetch_message=AsyncMock()),
            reference=reference,
            mentions=mentions or [],
            role_mentions=role_mentions or [],
            mention_everyone=mention_everyone,
        )

    async def _reason(
        self,
        bot: SimpleNamespace,
        message: SimpleNamespace,
    ) -> InvocationReason | None:
        return await bot.invocation_policy.reason_for(message, bot_user=bot.user)

    async def test_mention_reply_is_the_compatible_direct_mode(self) -> None:
        bot = self._bot(modes=("mention_reply",))
        message = self._message("<@99> hello", mentions=[SimpleNamespace(id=99)])

        self.assertIs(await self._reason(bot, message), InvocationReason.MENTION)

        # A direct reply remains identifiable as a reply even when Discord also
        # includes a mention of the replied-to bot.
        original = SimpleNamespace(author=SimpleNamespace(id=99))
        message.reference = SimpleNamespace(message_id=7, resolved=None)
        message.channel.fetch_message.return_value = original
        self.assertIs(await self._reason(bot, message), InvocationReason.REPLY)

    async def test_explicit_name_mode_does_not_match_discussion_of_the_name(self) -> None:
        bot = self._bot(modes=("explicit_name",))

        self.assertIs(
            await self._reason(bot, self._message("Nycti, can you help?")),
            InvocationReason.EXPLICIT_NAME,
        )
        self.assertIsNone(
            await self._reason(bot, self._message("What does Nycti mean?"))
        )

    async def test_ambient_mode_requires_allowlist_addressedness_and_cooldown(self) -> None:
        bot = self._bot(modes=("ambient",), ambient_channel_ids=(10,))
        message = self._message("What changed today?")

        self.assertIs(await self._reason(bot, message), InvocationReason.AMBIENT)
        self.assertIsNone(await self._reason(bot, message))
        self.assertIs(
            await self._reason(bot, self._message("What changed today?", user_id=21)),
            InvocationReason.AMBIENT,
        )
        self.assertIsNone(
            await self._reason(
                bot,
                self._message("What changed today?", channel_id=11),
            )
        )
        self.assertIsNone(
            await self._reason(bot, self._message("That was a good change", user_id=22))
        )
        self.assertTrue(
            bot.invocation_policy._ambient_cooldown.ready(channel_id=10, user_id=22)
        )
        self.assertFalse(
            bot.invocation_policy._ambient_attempt_cooldown.ready(
                channel_id=10,
                user_id=22,
            )
        )
        self.assertIsNone(
            await self._reason(
                bot,
                self._message(
                    "What changed today?",
                    mentions=[SimpleNamespace(id=44)],
                ),
            )
        )

    async def test_broadcast_and_role_mentions_never_trigger_mention_or_ambient(self) -> None:
        bot = self._bot(
            modes=("mention_reply", "ambient"),
            ambient_channel_ids=(10,),
        )

        self.assertIsNone(
            await self._reason(
                bot,
                self._message("What changed? @everyone", mention_everyone=True),
            )
        )
        self.assertIsNone(
            await self._reason(
                bot,
                self._message(
                    "What changed?",
                    user_id=21,
                    role_mentions=[SimpleNamespace(id=77)],
                ),
            )
        )

    async def test_ambient_mode_does_not_intercept_replies_to_people(self) -> None:
        bot = self._bot(
            modes=("mention_reply", "ambient"),
            ambient_channel_ids=(10,),
        )
        message = self._message(
            "What do you mean?",
            reference=SimpleNamespace(message_id=7, resolved=None),
        )
        message.channel.fetch_message.return_value = SimpleNamespace(
            author=SimpleNamespace(id=55)
        )

        self.assertIsNone(await self._reason(bot, message))

    async def test_all_modes_fail_closed_for_bots_dms_and_wrong_guilds(self) -> None:
        bot = self._bot(
            modes=("mention_reply", "explicit_name", "ambient"),
            ambient_channel_ids=(10,),
        )

        self.assertIsNone(
            await self._reason(
                bot,
                self._message("Nycti, help", author_is_bot=True),
            )
        )
        self.assertIsNone(
            await self._reason(bot, self._message("Nycti, help", guild_id=None))
        )
        self.assertIsNone(
            await self._reason(bot, self._message("Nycti, help", guild_id=2))
        )
