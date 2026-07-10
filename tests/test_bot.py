import unittest
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nycti.chat.run_state import AnswerProfile
from nycti.discord.core import (
    format_runtime_preference_status,
    register_core_commands,
    set_user_depth_preference,
)
from nycti.discord.invocation import InvocationReason
from nycti.discord.help import format_help_message
from nycti.formatting import (
    IMAGE_ANALYSIS_UNAVAILABLE,
    NO_IMAGE_ANALYSIS,
    append_debug_block,
    build_multimodal_user_content,
    extract_image_attachment_urls,
    parse_discord_message_links,
    extract_think_content,
    format_channel_alias_list,
    format_current_date_context,
    format_discord_message_link,
    format_current_datetime_context,
    format_latency_debug_block,
    format_memory_debug_block,
    format_ping_message,
    format_reminder_list,
    format_thinking_block,
    normalize_discord_math,
    normalize_discord_tables,
    parse_json_object_payload,
    render_custom_emoji_aliases,
    should_include_images_in_chat_request,
    split_message_chunks,
    strip_think_blocks,
)


class BotUtilitiesTests(unittest.TestCase):
    def test_isolated_benchmark_reply_uses_no_discord_context_or_memory_side_effects(self) -> None:
        from nycti.bot import BENCHMARK_USER_ID, NyctiBot

        bot = object.__new__(NyctiBot)
        bot.settings = SimpleNamespace(
            openai_chat_model="test-chat",
            openai_memory_model="test-memory",
            openai_vision_model=None,
            channel_context_limit=10,
        )
        bot.database = SimpleNamespace(
            session=Mock(side_effect=AssertionError("benchmark opened a database session"))
        )
        bot._chat_context_builder = SimpleNamespace(
            prepare=AsyncMock(side_effect=AssertionError("benchmark loaded user context"))
        )
        bot._vision_context_service = SimpleNamespace()
        bot._chat_orchestrator = SimpleNamespace(
            run_chat_with_tools=AsyncMock(return_value=("benchmark answer", []))
        )
        bot._background_memory_writer = SimpleNamespace(schedule=Mock())

        with patch("nycti.bot.get_system_prompt", return_value="system prompt"):
            reply, metrics = asyncio.run(
                bot._generate_reply(
                    guild_id=111,
                    channel_id=222,
                    user_id=333,
                    user_name="Real User",
                    user_global_name="Real Global Name",
                    prompt="Latest LumenOS release?",
                    context_lines=["private Discord history"],
                    image_attachment_urls=[],
                    image_context_lines=["private image context"],
                    source_message_id=444,
                    mentioned_user_ids=[555],
                    collect_latency_debug=True,
                    isolated_benchmark=True,
                    isolated_benchmark_now=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
                )
            )

        self.assertEqual("benchmark answer", reply)
        self.assertEqual("yes", metrics["benchmark_isolated"] if metrics else None)
        bot.database.session.assert_not_called()
        bot._chat_context_builder.prepare.assert_not_awaited()
        bot._background_memory_writer.schedule.assert_not_called()
        call = bot._chat_orchestrator.run_chat_with_tools.await_args.kwargs
        self.assertIsNone(call["guild_id"])
        self.assertIsNone(call["channel_id"])
        self.assertEqual(BENCHMARK_USER_ID, call["user_id"])
        self.assertIsNone(call["source_message_id"])
        rendered_prompt = call["messages"][1]["content"]
        self.assertIsInstance(rendered_prompt, str)
        self.assertIn(f"Current user: benchmark (id={BENCHMARK_USER_ID})", rendered_prompt)
        self.assertIn("Latest LumenOS release?", rendered_prompt)
        self.assertIn("July 10, 2026", rendered_prompt)
        self.assertNotIn("Real User", rendered_prompt)
        self.assertNotIn("Real Global Name", rendered_prompt)
        self.assertNotIn("private Discord history", rendered_prompt)
        self.assertNotIn("private image context", rendered_prompt)

    def test_reply_can_disable_memory_persistence_without_skipping_context_preparation(self) -> None:
        from nycti.bot import NyctiBot

        class SessionContext:
            def __init__(self, session: object) -> None:
                self.session = session

            async def __aenter__(self) -> object:
                return self.session

            async def __aexit__(self, *_args: object) -> None:
                return None

        session = SimpleNamespace(commit=AsyncMock())
        prepared_context = SimpleNamespace(
            current_datetime_text="Friday, July 10, 2026",
            memories_block="(none)",
            personal_profile_block="(none)",
            channel_alias_block="(none configured)",
            member_alias_block="(none matched)",
            mentioned_user_memories_block="(none)",
            memory_enabled=False,
            retrieved_memories=[],
            memory_retrieval_ms=0,
        )
        bot = object.__new__(NyctiBot)
        bot.settings = SimpleNamespace(
            openai_chat_model="test-chat",
            openai_memory_model="test-memory",
            openai_vision_model=None,
            channel_context_limit=10,
            discord_admin_user_id=None,
        )
        bot.database = SimpleNamespace(session=Mock(return_value=SessionContext(session)))
        bot._chat_context_builder = SimpleNamespace(
            prepare=AsyncMock(return_value=prepared_context)
        )
        bot._vision_context_service = SimpleNamespace()
        bot._chat_orchestrator = SimpleNamespace(
            run_chat_with_tools=AsyncMock(return_value=("benchmark answer", []))
        )
        bot._background_memory_writer = SimpleNamespace(schedule=Mock())

        with patch("nycti.bot.get_system_prompt", return_value="system prompt"):
            asyncio.run(
                bot._generate_reply(
                    guild_id=111,
                    channel_id=222,
                    user_id=333,
                    user_name="Real User",
                    user_global_name="Real User",
                    prompt="fixture request",
                    context_lines=[],
                    image_attachment_urls=[],
                    image_context_lines=[],
                    source_message_id=None,
                    include_memories=False,
                    persist_memory=False,
                )
            )

        bot.database.session.assert_called_once_with()
        bot._chat_context_builder.prepare.assert_awaited_once()
        session.commit.assert_awaited_once_with()
        bot._background_memory_writer.schedule.assert_not_called()

    def test_context_mentions_exclude_nycti_other_bots_and_duplicates(self) -> None:
        from nycti.bot import select_human_mentioned_user_ids

        mentions = [
            SimpleNamespace(id=99, bot=True),
            SimpleNamespace(id=10, bot=False),
            SimpleNamespace(id=11, bot=True),
            SimpleNamespace(id=10, bot=False),
        ]

        self.assertEqual(
            [10],
            select_human_mentioned_user_ids(mentions, bot_user_id=99),
        )

    def test_depth_preference_is_per_user_and_auto_clears_it(self) -> None:
        bot = SimpleNamespace(
            _depth_preferences={},
            _latency_debug_enabled_users=set(),
            _memory_debug_enabled_users=set(),
            _thinking_enabled_users=set(),
        )

        selected = set_user_depth_preference(bot, user_id=10, mode="deep")

        self.assertEqual(AnswerProfile.DEEP, selected)
        self.assertEqual({10: AnswerProfile.DEEP}, bot._depth_preferences)
        self.assertIn("answer depth: `deep`", format_runtime_preference_status(bot, user_id=10))
        self.assertIn("answer depth: `auto`", format_runtime_preference_status(bot, user_id=11))

        self.assertIsNone(set_user_depth_preference(bot, user_id=10, mode="auto"))
        self.assertEqual({}, bot._depth_preferences)

    def test_depth_command_sets_status_and_reset_clears_preference(self) -> None:
        class FakeTree:
            def __init__(self) -> None:
                self.commands: dict[str, object] = {}

            def command(self, *, name: str, **_kwargs):  # type: ignore[no-untyped-def]
                def decorator(callback):  # type: ignore[no-untyped-def]
                    self.commands[name] = callback
                    return callback

                return decorator

            def add_command(self, _command, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

        tree = FakeTree()
        bot = SimpleNamespace(
            tree=tree,
            latency=0.1,
            _depth_preferences={},
            _latency_debug_enabled_users=set(),
            _memory_debug_enabled_users=set(),
            _thinking_enabled_users=set(),
            _active_requests=SimpleNamespace(
                cancel=Mock(return_value=True),
                cancel_all=lambda: 0,
            ),
        )
        register_core_commands(bot)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            response=response,
        )

        asyncio.run(tree.commands["depth"](interaction, "grounded"))  # type: ignore[operator]

        self.assertEqual(AnswerProfile.GROUNDED, bot._depth_preferences[42])
        self.assertIn("`grounded`", response.send_message.await_args.args[0])

        response.send_message.reset_mock()
        asyncio.run(tree.commands["depth"](interaction, None))  # type: ignore[operator]
        self.assertIn("answer depth: `grounded`", response.send_message.await_args.args[0])

        response.send_message.reset_mock()
        asyncio.run(tree.commands["cancel"](interaction))  # type: ignore[operator]
        bot._active_requests.cancel.assert_called_once_with((2, 42))
        self.assertIn("Cancelling", response.send_message.await_args.args[0])

        response.send_message.reset_mock()
        with patch("nycti.discord.core.can_manage_guild", return_value=True):
            asyncio.run(tree.commands["reset"](interaction))  # type: ignore[operator]
        self.assertEqual({}, bot._depth_preferences)
        self.assertIn("cleared per-user preferences", response.send_message.await_args.args[0])
        self.assertTrue(
            getattr(tree.commands["reset"], "__discord_app_commands_guild_only__", False)
        )

    def test_reset_rejects_dm_without_clearing_runtime_state(self) -> None:
        class FakeTree:
            def __init__(self) -> None:
                self.commands: dict[str, object] = {}

            def command(self, *, name: str, **_kwargs):  # type: ignore[no-untyped-def]
                def decorator(callback):  # type: ignore[no-untyped-def]
                    self.commands[name] = callback
                    return callback

                return decorator

            def add_command(self, _command, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

        tree = FakeTree()
        bot = SimpleNamespace(
            tree=tree,
            latency=0.1,
            _depth_preferences={42: AnswerProfile.DEEP},
            _latency_debug_enabled_users={42},
            _memory_debug_enabled_users=set(),
            _thinking_enabled_users=set(),
            _active_requests=SimpleNamespace(cancel_all=Mock(return_value=1)),
        )
        register_core_commands(bot)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            guild=None,
            response=response,
        )

        asyncio.run(tree.commands["reset"](interaction))  # type: ignore[operator]

        self.assertEqual({42: AnswerProfile.DEEP}, bot._depth_preferences)
        bot._active_requests.cancel_all.assert_not_called()
        self.assertEqual("This command only works in a server channel.", response.send_message.await_args.args[0])

    def test_unknown_user_type_cannot_manage_guild(self) -> None:
        from nycti.discord.common import can_manage_guild

        self.assertFalse(can_manage_guild(None))
        self.assertFalse(can_manage_guild(SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))))

    def test_command_tree_rejects_dm_and_wrong_configured_guild(self) -> None:
        import discord

        from nycti.bot import NyctiCommandTree

        tree = object.__new__(NyctiCommandTree)
        tree.client = SimpleNamespace(settings=SimpleNamespace(discord_guild_id=123))

        wrong_response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        wrong_interaction = SimpleNamespace(
            guild_id=999,
            type=discord.InteractionType.application_command,
            response=wrong_response,
        )
        self.assertFalse(asyncio.run(tree.interaction_check(wrong_interaction)))
        self.assertEqual(
            "This bot is configured for a different server.",
            wrong_response.send_message.await_args.args[0],
        )

        dm_response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        dm_interaction = SimpleNamespace(
            guild_id=None,
            type=discord.InteractionType.application_command,
            response=dm_response,
        )
        self.assertFalse(asyncio.run(tree.interaction_check(dm_interaction)))
        self.assertEqual(
            "This command only works in a server channel.",
            dm_response.send_message.await_args.args[0],
        )

        allowed_response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        allowed_interaction = SimpleNamespace(
            guild_id=123,
            type=discord.InteractionType.application_command,
            response=allowed_response,
        )
        self.assertTrue(asyncio.run(tree.interaction_check(allowed_interaction)))
        allowed_response.send_message.assert_not_awaited()

        autocomplete_response = SimpleNamespace(
            is_done=lambda: False,
            autocomplete=AsyncMock(),
        )
        autocomplete_interaction = SimpleNamespace(
            guild_id=999,
            type=discord.InteractionType.autocomplete,
            response=autocomplete_response,
        )
        self.assertFalse(asyncio.run(tree.interaction_check(autocomplete_interaction)))
        autocomplete_response.autocomplete.assert_awaited_once_with([])

    def test_message_handling_ignores_other_guilds_when_configured(self) -> None:
        from nycti.bot import NyctiBot

        bot = SimpleNamespace(
            settings=SimpleNamespace(discord_guild_id=123),
            user=SimpleNamespace(id=9),
            _invocation_policy=SimpleNamespace(
                reason_for=AsyncMock(return_value=None),
            ),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            guild=SimpleNamespace(id=999),
            content="hello",
        )

        asyncio.run(NyctiBot.on_message(bot, message))
        bot._invocation_policy.reason_for.assert_not_awaited()

        message.guild.id = 123
        asyncio.run(NyctiBot.on_message(bot, message))
        bot._invocation_policy.reason_for.assert_awaited_once_with(
            message,
            bot_user=bot.user,
        )

    def test_unaddressed_bad_bot_does_not_bypass_invocation_policy(self) -> None:
        from nycti.bot import NyctiBot

        bot = SimpleNamespace(
            settings=SimpleNamespace(discord_guild_id=123),
            user=SimpleNamespace(id=9),
            _invocation_policy=SimpleNamespace(
                reason_for=AsyncMock(return_value=None),
            ),
            _handle_bad_bot_feedback=AsyncMock(return_value=True),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            guild=SimpleNamespace(id=123),
            content="bad bot",
        )

        asyncio.run(NyctiBot.on_message(bot, message))

        bot._handle_bad_bot_feedback.assert_not_awaited()

    def test_bad_bot_shortcut_requires_an_accepted_reply(self) -> None:
        from nycti.bot import NyctiBot

        bot = SimpleNamespace(
            settings=SimpleNamespace(discord_guild_id=123),
            user=SimpleNamespace(id=9),
            _invocation_policy=SimpleNamespace(
                reason_for=AsyncMock(return_value=InvocationReason.REPLY),
            ),
            _handle_bad_bot_feedback=AsyncMock(return_value=True),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            guild=SimpleNamespace(id=123),
            content="bad bot: stale answer",
        )

        asyncio.run(NyctiBot.on_message(bot, message))

        bot._handle_bad_bot_feedback.assert_awaited_once_with(message)

    def test_plsfix_requires_an_explicit_exact_admin_id(self) -> None:
        from nycti.bot import NyctiBot

        message = SimpleNamespace(
            author=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            id=3,
            jump_url="https://discord.com/channels/1/2/3",
            reply=AsyncMock(),
        )
        bot = SimpleNamespace(
            settings=SimpleNamespace(
                discord_admin_user_id=None,
                error_debug_channel_id=9,
            ),
            database=SimpleNamespace(),
        )

        with patch("nycti.bot.send_plsfix_diagnostics", new=AsyncMock(return_value=True)) as send:
            asyncio.run(NyctiBot._handle_plsfix_request(bot, message, "plsfix"))
            send.assert_not_awaited()
            self.assertIn("disabled", message.reply.await_args.args[0])

            message.reply.reset_mock()
            bot.settings.discord_admin_user_id = 7
            asyncio.run(NyctiBot._handle_plsfix_request(bot, message, "plsfix"))
            send.assert_not_awaited()
            self.assertIn("admin-only", message.reply.await_args.args[0])

            message.reply.reset_mock()
            bot.settings.discord_admin_user_id = 42
            asyncio.run(NyctiBot._handle_plsfix_request(bot, message, "plsfix"))
            send.assert_awaited_once()
            self.assertIn("Captured", message.reply.await_args.args[0])

    def test_format_ping_message_rounds_to_milliseconds(self) -> None:
        self.assertEqual(format_ping_message(0.1234), "Pong! `123 ms`")

    def test_format_ping_message_clamps_negative_latency(self) -> None:
        self.assertEqual(format_ping_message(-1.0), "Pong! `0 ms`")

    def test_typing_heartbeat_repeats_until_done_event(self) -> None:
        try:
            from nycti import bot as bot_module
            from nycti.bot import _send_typing_while_pending
        except ModuleNotFoundError as exc:
            self.skipTest(f"Optional bot runtime dependency is not installed: {exc.name}")

        async def run_test() -> int:
            channel = FakeChannel()
            done = asyncio.Event()
            original_interval = bot_module.TYPING_HEARTBEAT_SECONDS
            bot_module.TYPING_HEARTBEAT_SECONDS = 0.01
            try:
                task = asyncio.create_task(_send_typing_while_pending(channel, done))
                await asyncio.sleep(0.025)
                done.set()
                await asyncio.wait_for(task, timeout=1)
            finally:
                bot_module.TYPING_HEARTBEAT_SECONDS = original_interval
            return channel.typing_calls

        class FakeChannel:
            def __init__(self) -> None:
                self.typing_calls = 0

            async def trigger_typing(self) -> None:
                self.typing_calls += 1

        self.assertGreaterEqual(asyncio.run(run_test()), 2)

    def test_delayed_progress_can_be_claimed_and_edited_into_final_reply(self) -> None:
        from unittest.mock import AsyncMock

        from nycti.bot import NyctiBot, _claim_delayed_progress, _send_delayed_progress

        progress_message = SimpleNamespace(edit=AsyncMock())
        progress_message.edit.return_value = progress_message
        source_message = SimpleNamespace(
            reply=AsyncMock(return_value=progress_message),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        bot = object.__new__(NyctiBot)

        async def run_test() -> list[object]:
            task = asyncio.create_task(
                _send_delayed_progress(source_message, delay_seconds=0)
            )
            await task
            claimed = await _claim_delayed_progress(task)
            return await bot._send_message_reply_chunks(
                source_message,
                "Final answer.",
                progress_message=claimed,
            )

        sent = asyncio.run(run_test())

        source_message.reply.assert_awaited_once()
        progress_message.edit.assert_awaited_once()
        edit_kwargs = progress_message.edit.await_args.kwargs
        self.assertEqual("Final answer.", edit_kwargs["content"])
        allowed_mentions = edit_kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.users)
        self.assertEqual([progress_message], sent)

    def test_fast_reply_cancels_delayed_progress_before_posting(self) -> None:
        from unittest.mock import AsyncMock

        from nycti.bot import _claim_delayed_progress, _send_delayed_progress

        source_message = SimpleNamespace(reply=AsyncMock())

        async def run_test() -> object | None:
            task = asyncio.create_task(
                _send_delayed_progress(source_message, delay_seconds=60)
            )
            return await _claim_delayed_progress(task)

        self.assertIsNone(asyncio.run(run_test()))
        source_message.reply.assert_not_awaited()

    def test_format_error_debug_message_sanitizes_and_includes_metadata(self) -> None:
        try:
            from nycti.error_debug import format_error_debug_message
        except ModuleNotFoundError as exc:
            self.skipTest(f"Optional bot runtime dependency is not installed: {exc.name}")

        message = format_error_debug_message(
            kind="provider_recovery",
            source_channel_id=123,
            source_message_id=456,
            source_user_id=789,
            source_message_url="https://discord.com/channels/1/123/456",
            detail="native tool request was rejected ``` secret fence",
            metrics={
                "active_chat_model": "model-a",
                "native_tool_fallback_count": 1,
                "exposed_tools": "web_search",
            },
        )

        self.assertIn("nycti_error_debug", message)
        self.assertIn("type: provider_recovery", message)
        self.assertIn("source_message_id: 456", message)
        self.assertIn("active_chat_model: model-a", message)
        self.assertIn("native_tool_fallback_count: 1", message)
        self.assertNotIn("``` secret fence", message)

    def test_plsfix_request_detection_accepts_discord_style_phrases(self) -> None:
        try:
            from nycti.diagnostics import is_plsfix_request
        except ModuleNotFoundError as exc:
            self.skipTest(f"Optional bot runtime dependency is not installed: {exc.name}")

        self.assertTrue(is_plsfix_request("plsfix"))
        self.assertTrue(is_plsfix_request("nycti pls fix this"))
        self.assertTrue(is_plsfix_request("please fix"))
        self.assertFalse(is_plsfix_request("can you fix this answer?"))

    def test_bad_bot_feedback_handler_logs_recent_response_without_llm(self) -> None:
        from unittest.mock import AsyncMock, patch

        from nycti.bot import NyctiBot
        from nycti.feedback import ResponseDiagnosticCache, ResponseDiagnosticSnapshot

        now = datetime.now(timezone.utc)
        bot = object.__new__(NyctiBot)
        bot._response_diagnostic_cache = ResponseDiagnosticCache()
        bot.database = SimpleNamespace()
        bot.settings = SimpleNamespace(error_debug_channel_id=99)
        bot._response_diagnostic_cache.record(
            ResponseDiagnosticSnapshot(
                captured_at=now,
                guild_id=1,
                channel_id=2,
                source_message_id=3,
                source_message_url="https://discord.com/channels/1/2/3",
                source_user_id=4,
                prompt="request",
                context_lines=(),
                image_context_lines=(),
                reply_text="reply",
                metrics={"agent_run_id": "run"},
            ),
            bot_message_ids=[10],
        )
        message = SimpleNamespace(
            id=11,
            content="bad bot",
            jump_url="https://discord.com/channels/1/2/11",
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=5),
            reference=SimpleNamespace(message_id=10),
            reply=AsyncMock(),
        )

        with patch("nycti.bot.send_bad_bot_feedback", new=AsyncMock(return_value=True)) as send:
            handled = asyncio.run(bot._handle_bad_bot_feedback(message))

        self.assertTrue(handled)
        send.assert_awaited_once()
        message.reply.assert_awaited_once_with(
            "Logged that response for review.",
            mention_author=False,
        )

    def test_bad_bot_feedback_handler_explains_missing_recent_reply(self) -> None:
        from unittest.mock import AsyncMock

        from nycti.bot import NyctiBot
        from nycti.feedback import ResponseDiagnosticCache

        bot = object.__new__(NyctiBot)
        bot._response_diagnostic_cache = ResponseDiagnosticCache()
        bot.database = SimpleNamespace()
        bot.settings = SimpleNamespace(error_debug_channel_id=99)
        message = SimpleNamespace(
            content="bad bot",
            channel=SimpleNamespace(id=2),
            guild=SimpleNamespace(id=1),
            reference=SimpleNamespace(message_id=10),
            reply=AsyncMock(),
        )

        handled = asyncio.run(bot._handle_bad_bot_feedback(message))

        self.assertTrue(handled)
        message.reply.assert_awaited_once_with(
            "I couldn't find a Nycti reply from the last 15 minutes to log. Reply directly to it and try again.",
            mention_author=False,
        )

    def test_bad_bot_feedback_handler_reports_debug_delivery_failure(self) -> None:
        from unittest.mock import AsyncMock, patch

        from nycti.bot import NyctiBot
        from nycti.feedback import ResponseDiagnosticCache, ResponseDiagnosticSnapshot

        bot = object.__new__(NyctiBot)
        bot._response_diagnostic_cache = ResponseDiagnosticCache()
        bot.database = SimpleNamespace()
        bot.settings = SimpleNamespace(error_debug_channel_id=99)
        bot._response_diagnostic_cache.record(
            ResponseDiagnosticSnapshot(
                captured_at=datetime.now(timezone.utc),
                guild_id=1,
                channel_id=2,
                source_message_id=3,
                source_message_url="https://discord.com/channels/1/2/3",
                source_user_id=4,
                prompt="request",
                context_lines=(),
                image_context_lines=(),
                reply_text="reply",
                metrics={"agent_run_id": "run"},
            ),
            bot_message_ids=[10],
        )
        message = SimpleNamespace(
            content="bad bot",
            channel=SimpleNamespace(id=2),
            guild=SimpleNamespace(id=1),
            reference=SimpleNamespace(message_id=10),
            reply=AsyncMock(),
        )

        with patch("nycti.bot.send_bad_bot_feedback", new=AsyncMock(return_value=False)):
            handled = asyncio.run(bot._handle_bad_bot_feedback(message))

        self.assertTrue(handled)
        message.reply.assert_awaited_once_with(
            "I found that response, but couldn't send its diagnostics to the debug channel.",
            mention_author=False,
        )

    def test_send_error_debug_message_attaches_payload_file(self) -> None:
        try:
            from nycti.error_debug import send_error_debug_message
        except ModuleNotFoundError as exc:
            self.skipTest(f"Optional bot runtime dependency is not installed: {exc.name}")

        class FakeChannel:
            def __init__(self) -> None:
                self.sent: dict[str, object] | None = None

            async def send(self, content: str, **kwargs: object) -> None:
                self.sent = {"content": content, **kwargs}

        class FakeBot:
            def __init__(self, channel: FakeChannel) -> None:
                self.channel = channel

            def get_channel(self, channel_id: int) -> FakeChannel:
                return self.channel

        async def run_test() -> dict[str, object]:
            channel = FakeChannel()
            delivered = await send_error_debug_message(
                FakeBot(channel),
                channel_id=123,
                content="debug",
                attachment_text='{"messages":[]}',
                attachment_filename="request.json",
            )
            assert delivered
            assert channel.sent is not None
            return channel.sent

        sent = asyncio.run(run_test())

        self.assertEqual(sent["content"], "debug")
        self.assertEqual(getattr(sent["file"], "filename"), "request.json")

    def test_extract_image_attachment_urls_filters_non_images_and_limits_count(self) -> None:
        attachments = [
            SimpleNamespace(content_type="image/png", filename="chart.png", url="https://cdn.example.com/a.png"),
            SimpleNamespace(content_type="text/plain", filename="notes.txt", url="https://cdn.example.com/notes.txt"),
            SimpleNamespace(content_type="", filename="photo.jpeg", url="https://cdn.example.com/b.jpeg"),
            SimpleNamespace(content_type="image/webp", filename="meme.webp", url="https://cdn.example.com/c.webp"),
            SimpleNamespace(content_type="image/gif", filename="clip.gif", url="https://cdn.example.com/d.gif"),
        ]
        self.assertEqual(
            extract_image_attachment_urls(attachments),
            [
                "https://cdn.example.com/a.png",
                "https://cdn.example.com/b.jpeg",
                "https://cdn.example.com/c.webp",
            ],
        )

    def test_build_multimodal_user_content_wraps_text_and_images(self) -> None:
        content = build_multimodal_user_content("look at this chart", ["https://cdn.example.com/chart.png"])
        self.assertIsInstance(content, list)
        assert isinstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "look at this chart"})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "https://cdn.example.com/chart.png"}},
        )

    def test_should_include_images_in_chat_request_uses_chat_model_when_no_vision_model(self) -> None:
        self.assertTrue(
            should_include_images_in_chat_request(
                ["https://cdn.example.com/chart.png"],
                vision_model=None,
                vision_context_block=NO_IMAGE_ANALYSIS,
            )
        )

    def test_should_include_images_in_chat_request_skips_chat_model_when_vision_prepass_succeeds(self) -> None:
        self.assertFalse(
            should_include_images_in_chat_request(
                ["https://cdn.example.com/chart.png"],
                vision_model="gpt-4.1-mini-vision",
                vision_context_block="image 1 shows a green chart",
            )
        )

    def test_should_include_images_in_chat_request_avoids_prepass_for_same_model(self) -> None:
        self.assertTrue(
            should_include_images_in_chat_request(
                ["https://cdn.example.com/chart.png"],
                vision_model="gpt-5.6-luna",
                chat_model="GPT-5.6-Luna",
                vision_context_block=NO_IMAGE_ANALYSIS,
            )
        )

    def test_should_include_images_in_chat_request_falls_back_when_vision_prepass_fails(self) -> None:
        self.assertTrue(
            should_include_images_in_chat_request(
                ["https://cdn.example.com/chart.png"],
                vision_model="https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5",
                vision_context_block=IMAGE_ANALYSIS_UNAVAILABLE,
            )
        )

    def test_should_include_images_in_chat_request_requires_images(self) -> None:
        self.assertFalse(
            should_include_images_in_chat_request(
                [],
                vision_model=None,
                vision_context_block=NO_IMAGE_ANALYSIS,
            )
        )

    def test_parse_discord_message_links_extracts_same_guild_links(self) -> None:
        text = (
            "look at this https://discord.com/channels/123/456/789 and "
            "https://canary.discord.com/channels/123/456/790"
        )
        self.assertEqual(parse_discord_message_links(text, guild_id=123), [(456, 789), (456, 790)])

    def test_parse_discord_message_links_ignores_other_guilds_and_dedupes(self) -> None:
        text = (
            "https://discord.com/channels/999/456/789 "
            "https://discord.com/channels/123/456/789 "
            "https://discord.com/channels/123/456/789"
        )
        self.assertEqual(parse_discord_message_links(text, guild_id=123), [(456, 789)])

    def test_format_help_message_mentions_core_commands_and_tips(self) -> None:
        help_page_one = format_help_message(1)
        help_page_two = format_help_message(2)
        self.assertIn("/help page:<1-2>", help_page_one)
        self.assertIn("/ping", help_page_one)
        self.assertIn("/depth [mode:<quick|grounded|deep|auto>]", help_page_one)
        self.assertIn("/benchmark suite|failures|trace", help_page_one)
        self.assertIn("/cancel", help_page_one)
        self.assertIn("/logs [period:<day|week|custom>] [hours]", help_page_one)
        self.assertIn("/memories [userid:<id>]", help_page_one)
        self.assertIn("/memory enable:<true|false>", help_page_one)
        self.assertIn("/memory forget:<id>", help_page_one)
        self.assertIn("ask naturally", help_page_two)
        self.assertNotIn("fast search", help_page_two)
        self.assertIn("plsfix", help_page_two)
        self.assertTrue(all(len(page) <= 2000 for page in (help_page_one, help_page_two)))

    def test_format_latency_debug_block_contains_expected_keys(self) -> None:
        block = format_latency_debug_block(
            {
                "chat_model": "gpt-4.1-mini",
                "vision_model": "gpt-4.1-vision",
                "active_chat_model": "gpt-4.1-vision",
                "memory_model": "gpt-4.1-nano",
                "chat_prompt_tokens": 1200,
                "chat_completion_tokens": 300,
                "chat_total_tokens": 1500,
                "end_to_end_ms": 1000,
                "context_fetch_ms": 40,
                "channel_context_mode": "summary",
                "channel_context_multiplier": 2,
                "channel_context_status": "ok",
                "channel_context_fetch_count": 1,
                "channel_context_fetch_ms": 35,
                "channel_context_summary_tokens": 220,
                "memory_retrieval_ms": 30,
                "vision_summary_ms": 55,
                "native_tool_fallback_count": 1,
                "provider_recovery_notice": "native tool request was rejected; switched to plain/XML tool fallback",
                "tool_call_count": 3,
                "market_data_provider": "twelvedata",
                "stock_quote_symbols": "SPX, ES",
                "stock_quote_symbol_count": 2,
                "stock_quote_status": "mixed",
                "stock_quote_error": "Market quote for `SPX` failed because the Twelve Data request failed.",
                "stock_quote_count": 1,
                "stock_quote_ms": 45,
                "price_history_symbol": "SPY",
                "price_history_interval": "1day",
                "price_history_status": "ok",
                "price_history_error": "",
                "price_history_count": 1,
                "price_history_ms": 32,
                "web_search_query_count": 2,
                "web_search_ms": 120,
                "duplicate_tool_call_count": 1,
                "chat_empty_turn_count": 1,
                "chat_empty_turn_feature": "chat_reply",
                "chat_empty_final_count": 1,
                "chat_final_failure_count": 1,
                "chat_final_failure_reason": "empty",
                "chat_final_failure_error": "RuntimeError: provider unavailable",
                "chat_final_raw_output_kind": "tavily_dump",
                "chat_llm_ms": 800,
                "chat_usage_write_ms": 5,
                "chat_commit_ms": 10,
                "reply_generation_ms": 900,
                "agent_run_id": "run-123",
                "agent_model_turn_count": 2,
                "agent_tool_call_count": 3,
                "agent_stop_reason": "final_text",
            }
        )
        self.assertIn("latency_debug_ms", block)
        self.assertIn("chat_model: gpt-4.1-mini", block)
        self.assertIn("vision_model: gpt-4.1-vision", block)
        self.assertIn("active_chat_model: gpt-4.1-vision", block)
        self.assertIn("memory_model: gpt-4.1-nano", block)
        self.assertIn("chat_prompt_tokens: 1200", block)
        self.assertIn("chat_completion_tokens: 300", block)
        self.assertIn("chat_total_tokens: 1500", block)
        self.assertIn("chat_tokens_per_s: 375.0", block)
        self.assertIn("end_to_end_ms: 1000", block)
        self.assertIn("channel_context_mode: summary", block)
        self.assertIn("channel_context_multiplier: 2", block)
        self.assertIn("channel_context_status: ok", block)
        self.assertIn("channel_context_summary_tokens: 220", block)
        self.assertIn("vision_summary_ms: 55", block)
        self.assertIn("native_tool_fallback_count: 1", block)
        self.assertIn(
            "provider_recovery_notice: native tool request was rejected; switched to plain/XML tool fallback",
            block,
        )
        self.assertIn("tool_call_count: 3", block)
        self.assertIn("market_data_provider: twelvedata", block)
        self.assertIn("stock_quote_symbols: SPX, ES", block)
        self.assertIn("stock_quote_symbol_count: 2", block)
        self.assertIn("stock_quote_status: mixed", block)
        self.assertIn("stock_quote_error: Market quote for `SPX` failed because the Twelve Data request failed.", block)
        self.assertIn("stock_quote_count: 1", block)
        self.assertIn("price_history_symbol: SPY", block)
        self.assertIn("price_history_interval: 1day", block)
        self.assertIn("price_history_status: ok", block)
        self.assertIn("price_history_count: 1", block)
        self.assertIn("web_search_query_count: 2", block)
        self.assertIn("duplicate_tool_call_count: 1", block)
        self.assertIn("chat_empty_turn_count: 1", block)
        self.assertIn("chat_empty_turn_feature: chat_reply", block)
        self.assertIn("chat_empty_final_count: 1", block)
        self.assertIn("chat_final_failure_count: 1", block)
        self.assertIn("chat_final_failure_reason: empty", block)
        self.assertIn("chat_final_failure_error: RuntimeError: provider unavailable", block)
        self.assertIn("chat_final_raw_output_kind: tavily_dump", block)
        self.assertIn("agent_run_id: run-123", block)
        self.assertIn("agent_model_turn_count: 2", block)
        self.assertIn("agent_tool_call_count: 3", block)
        self.assertIn("agent_stop_reason: final_text", block)
        self.assertIn("memory_extraction: background", block)

    def test_format_memory_debug_block_contains_retrieved_memories(self) -> None:
        block = format_memory_debug_block(
            memory_enabled=True,
            memory_retrieval_ms=24,
            embedding_model="text-embedding-3-large",
            embedding_api_key_mode="separate-configured",
            embedding_base_url_mode="openai-default",
            memories=[
                SimpleNamespace(category="plan", summary="Wants to get a job at Optiver"),
                SimpleNamespace(category="preference", summary="Prefers lowercase mat"),
            ],
        )
        self.assertIn("memory_debug", block)
        self.assertIn("memory_enabled: yes", block)
        self.assertIn("memory_retrieval_ms: 24", block)
        self.assertIn("embedding_model: text-embedding-3-large", block)
        self.assertIn("embedding_api_key: separate-configured", block)
        self.assertIn("embedding_base_url: openai-default", block)
        self.assertIn("retrieved_memory_count: 2", block)
        self.assertIn("[plan] Wants to get a job at Optiver", block)

    def test_append_debug_block_trims_reply_to_limit(self) -> None:
        reply = "x" * 1900
        debug_block = "```text\nsample\n```"
        merged = append_debug_block(reply, debug_block, limit=1900)
        self.assertLessEqual(len(merged), 1900)
        self.assertIn("sample", merged)

    def test_append_debug_block_can_skip_trimming(self) -> None:
        reply = "x" * 1900
        debug_block = "```text\nsample\n```"
        merged = append_debug_block(reply, debug_block, limit=None)
        self.assertGreater(len(merged), 1900)
        self.assertTrue(merged.endswith(debug_block))

    def test_split_message_chunks_keeps_all_content(self) -> None:
        text = ("alpha\n\n" + ("x" * 1500) + "\n\n" + ("y" * 1500)).strip()
        chunks = split_message_chunks(text, limit=1900)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text.replace("\n\n", ""))

    def test_strip_think_blocks_removes_reasoning_wrapper(self) -> None:
        text = "<think>internal reasoning</think>\n\nmorning mat! :wave:"
        self.assertEqual(strip_think_blocks(text), "morning mat! :wave:")

    def test_strip_think_blocks_handles_missing_blocks(self) -> None:
        text = "hello"
        self.assertEqual(strip_think_blocks(text), "hello")

    def test_extract_think_content_collects_multiple_blocks(self) -> None:
        text = "<think>first</think>\nhello\n<think>second</think>"
        self.assertEqual(extract_think_content(text), ["first", "second"])

    def test_format_thinking_block_quotes_reasoning(self) -> None:
        block = format_thinking_block(["step one", "step two"])
        self.assertIn("-# reasoning", block)
        self.assertIn("> step one", block)
        self.assertIn("> step two", block)

    def test_normalize_discord_tables_wraps_markdown_table_in_code_block(self) -> None:
        text = "| Name | Revenue |\n| --- | --- |\n| NVDA | $39.3B |\n| AMD | $7.7B |"
        normalized = normalize_discord_tables(text)
        self.assertTrue(normalized.startswith("```text\n"))
        self.assertIn("Name | Revenue", normalized)
        self.assertIn("NVDA | $39.3B", normalized)
        self.assertIn("-+-", normalized)

    def test_normalize_discord_tables_leaves_normal_text_unchanged(self) -> None:
        text = "Revenue was strong.\nGuidance was mixed."
        self.assertEqual(normalize_discord_tables(text), text)

    def test_normalize_discord_math_wraps_latex_display_block(self) -> None:
        text = (
            "Two-tailed probability:\n\n"
            "\\[\n"
            "P(|Z|>20) \\approx 5.6 \\times 10^{-89}\n"
            "\\]"
        )

        self.assertEqual(
            normalize_discord_math(text),
            (
                "Two-tailed probability:\n\n"
                "```text\n"
                "P(|Z|>20) \\approx 5.6 \\times 10^{-89}\n"
                "```"
            ),
        )

    def test_normalize_discord_math_wraps_dollar_display_block(self) -> None:
        text = "$$\nP(Z>20) = 2.8e-89\n$$"

        self.assertEqual(
            normalize_discord_math(text),
            "```text\nP(Z>20) = 2.8e-89\n```",
        )

    def test_render_custom_emoji_aliases_replaces_known_aliases(self) -> None:
        text = "this is scuffed :pepebeat: and funny :kekw:"
        rendered = render_custom_emoji_aliases(
            text,
            {"pepebeat": "<:pepebeat:111>", "kekw": "<:kekw:222>"},
        )
        self.assertEqual(rendered, "this is scuffed <:pepebeat:111> and funny <:kekw:222>")

    def test_render_custom_emoji_aliases_leaves_unknown_aliases(self) -> None:
        text = "hmm :unknown:"
        rendered = render_custom_emoji_aliases(text, {"pepeww": "<:pepeww:333>"})
        self.assertEqual(rendered, "hmm :unknown:")

    def test_format_current_datetime_context_includes_localized_date_time(self) -> None:
        rendered = format_current_datetime_context(
            datetime(2026, 3, 19, 20, 34, 56, tzinfo=timezone.utc),
            "America/Los_Angeles",
        )
        self.assertEqual(rendered, "Thursday, March 19, 2026 at 1:34 PM PDT")

    def test_format_current_date_context_uses_words_without_time(self) -> None:
        rendered = format_current_date_context(
            datetime(2026, 7, 10, 20, 34, 56, tzinfo=timezone.utc),
            "America/Chicago",
        )
        self.assertEqual(rendered, "Friday, July 10, 2026")

    def test_format_discord_message_link_uses_guild_channel_and_message_ids(self) -> None:
        link = format_discord_message_link(guild_id=123, channel_id=456, message_id=789)
        self.assertEqual(link, "https://discord.com/channels/123/456/789")

    def test_format_reminder_list_renders_jump_link(self) -> None:
        reminder = SimpleNamespace(
            id=12,
            guild_id=123,
            channel_id=456,
            user_id=789,
            source_message_id=321,
            remind_at=datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc),
            reminder_text="check NVDA earnings",
        )
        rendered = format_reminder_list([reminder], timezone_name="America/Los_Angeles")
        self.assertIn("`12`", rendered)
        self.assertIn("check NVDA earnings", rendered)
        self.assertIn("https://discord.com/channels/123/456/321", rendered)

    def test_format_reminder_list_can_include_owner_and_channel(self) -> None:
        reminder = SimpleNamespace(
            id=13,
            guild_id=123,
            channel_id=456,
            user_id=789,
            source_message_id=None,
            remind_at=datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc),
            reminder_text="roll the calls",
        )
        rendered = format_reminder_list([reminder], timezone_name="UTC", include_owner=True)
        self.assertIn("<@789>", rendered)
        self.assertIn("<#456>", rendered)

    def test_format_channel_alias_list_renders_aliases(self) -> None:
        alias = SimpleNamespace(alias="alerts", channel_id=456)
        rendered = format_channel_alias_list([alias])
        self.assertEqual(rendered, "`alerts` -> <#456> (`456`)")

    def test_parse_json_object_payload_handles_embedded_json(self) -> None:
        parsed = parse_json_object_payload('noise {"query": "latest nvda earnings"} trailing')
        self.assertEqual(parsed, {"query": "latest nvda earnings"})

    def test_parse_json_object_payload_rejects_non_object(self) -> None:
        parsed = parse_json_object_payload('["latest nvda earnings"]')
        self.assertIsNone(parsed)

if __name__ == "__main__":
    unittest.main()
