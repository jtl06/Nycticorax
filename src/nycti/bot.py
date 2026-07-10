from __future__ import annotations

import asyncio
from contextlib import suppress
from io import BytesIO
import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from nycti.channel_aliases import ChannelAliasService
from nycti.changelog_service import ChangelogService
from nycti.chat.context import ChatContextBuilder, build_user_prompt
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import AnswerProfile
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tools.schemas import GET_CHANNEL_CONTEXT_TOOL_NAME
from nycti.browser import BrowserClient
from nycti.config import Settings
from nycti.db.session import Database
from nycti.debug_summary import DAILY_LOG_SUMMARY_CHECK_SECONDS, post_daily_logs_summary_if_due
from nycti.diagnostics import DiagnosticRequest, is_plsfix_request, send_plsfix_diagnostics
from nycti.discord.common import (
    CONFIGURED_GUILD_ONLY_MESSAGE,
    SERVER_ONLY_MESSAGE,
    is_configured_guild,
)
from nycti.discord.registration import register_bot_commands
from nycti.error_debug import (
    send_finalization_failure_debug,
    send_provider_recovery_debug,
    send_reply_generation_error_debug,
)
from nycti.feedback import (
    ResponseDiagnosticCache,
    ResponseDiagnosticSnapshot,
    is_bad_bot_feedback,
    send_bad_bot_feedback,
)
from nycti.formatting import (
    NO_IMAGE_ANALYSIS,
    append_debug_block,
    build_multimodal_user_content,
    format_discord_message_link,
    format_latency_debug_block,
    format_memory_debug_block,
    format_thinking_block,
    normalize_discord_tables,
    normalize_discord_math,
    render_custom_emoji_aliases,
    should_include_images_in_chat_request,
    split_message_chunks,
    strip_think_blocks,
)
from nycti.llm.client import OpenAIClient
from nycti.member_aliases import MemberAliasService
from nycti.message_context import (
    MessageContextCollector,
    clean_trigger_content,
)
from nycti.memory.background import BackgroundMemoryWriter
from nycti.memory.service import MemoryService
from nycti.prompts import get_system_prompt
from nycti.request_control import ActiveRequestRegistry
from nycti.reminders.service import ReminderService
from nycti.table_images import extract_markdown_tables_as_images
from nycti.tavily.client import TavilyClient
from nycti.timing import elapsed_ms
from nycti.twelvedata.client import TwelveDataClient
from nycti.yahoo import YahooFinanceClient
from nycti.usage import (
    prune_action_idempotency_before,
    prune_agent_telemetry_before,
    prune_message_debug_events_before,
    prune_usage_events_before,
    record_message_debug_stats,
    record_usage,
)
from nycti.vision import VisionContextService
from nycti.youtube import YouTubeTranscriptClient

LOGGER = logging.getLogger(__name__)
ALLOWED_CUSTOM_EMOJI_ALIASES = ("pepebeat", "pepeww", "kekw", "javsigh")
MAX_REPLY_CHAIN_DEPTH = 3
MAX_LINKED_MESSAGE_COUNT = 3
MAX_CONTEXT_IMAGE_COUNT = 3
MAX_ANCHOR_CONTEXT_PER_SIDE = 1
TYPING_HEARTBEAT_SECONDS = 8.0
PROGRESS_MESSAGE_DELAY_SECONDS = 2.0
PROGRESS_MESSAGE_TEXT = "Working on it…"
USAGE_EVENTS_RETENTION_DAYS = 7
DELIVERED_REMINDER_RETENTION_DAYS = 30
MEMORY_RETENTION_NEVER_RETRIEVED_DAYS = 90
MEMORY_RETENTION_STALE_RETRIEVED_DAYS = 180
RETENTION_CHECK_INTERVAL_SECONDS = 86400


class NyctiCommandTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        configured_guild_id = self.client.settings.discord_guild_id
        if is_configured_guild(
            guild_id=interaction.guild_id,
            configured_guild_id=configured_guild_id,
        ):
            return True
        if not interaction.response.is_done():
            if interaction.type is discord.InteractionType.autocomplete:
                await interaction.response.autocomplete([])
            else:
                message = (
                    SERVER_ONLY_MESSAGE
                    if interaction.guild_id is None
                    else CONFIGURED_GUILD_ONLY_MESSAGE
                )
                await interaction.response.send_message(message, ephemeral=True)
        return False


class NyctiBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        llm_client: OpenAIClient,
        market_data_client: TwelveDataClient,
        tavily_client: TavilyClient,
        yahoo_finance_client: YahooFinanceClient | None = None,
        browser_client: BrowserClient | None = None,
        youtube_client: YouTubeTranscriptClient | None = None,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        member_alias_service: MemberAliasService,
        reminder_service: ReminderService,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=NyctiCommandTree,
        )
        self.settings = settings
        self.started_at_utc = datetime.now(timezone.utc)
        self.database = database
        self.llm_client = llm_client
        self.market_data_client = market_data_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.member_alias_service = member_alias_service
        self.reminder_service = reminder_service
        self._active_requests = ActiveRequestRegistry()
        self._chat_orchestrator = ChatOrchestrator(
            settings=settings,
            database=database,
            llm_client=llm_client,
            market_data_client=market_data_client,
            yahoo_finance_client=yahoo_finance_client,
            tavily_client=tavily_client,
            browser_client=browser_client,
            youtube_client=youtube_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            reminder_service=reminder_service,
            bot=self,
        )
        self._chat_context_builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            member_alias_service=member_alias_service,
        )
        self._message_context_collector = MessageContextCollector(
            bot=self,
            channel_context_limit=self.settings.channel_context_limit,
            max_reply_chain_depth=MAX_REPLY_CHAIN_DEPTH,
            max_linked_message_count=MAX_LINKED_MESSAGE_COUNT,
            max_context_image_count=MAX_CONTEXT_IMAGE_COUNT,
            anchor_context_per_side=MAX_ANCHOR_CONTEXT_PER_SIDE,
        )
        self._vision_context_service = VisionContextService(settings, llm_client)
        self._background_memory_writer = BackgroundMemoryWriter(
            settings=settings,
            database=database,
            memory_service=memory_service,
        )
        self._changelog_service = ChangelogService(
            bot=self,
            database=database,
            settings=settings,
        )
        self._latency_debug_enabled_users: set[int] = set()
        self._memory_debug_enabled_users: set[int] = set()
        self._thinking_enabled_users: set[int] = set()
        self._depth_preferences: dict[int, AnswerProfile] = {}
        self._response_diagnostic_cache = ResponseDiagnosticCache()
        self._reminder_poll_task: asyncio.Task[None] | None = None
        self._daily_log_summary_task: asyncio.Task[None] | None = None
        self._startup_changelog_task: asyncio.Task[None] | None = None
        self._last_retention_run_at: datetime | None = None
        self._register_commands()

    async def setup_hook(self) -> None:
        await self.database.init_models()
        await self._run_retention_maintenance(force=True)
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if self._reminder_poll_task is None:
            self._reminder_poll_task = asyncio.create_task(self._run_reminder_poll_loop())
        if self.settings.error_debug_channel_id is not None and self._daily_log_summary_task is None:
            self._daily_log_summary_task = asyncio.create_task(self._run_daily_log_summary_loop())
        if self._startup_changelog_task is None:
            self._startup_changelog_task = asyncio.create_task(self._post_startup_changelog())

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def close(self) -> None:
        if self._reminder_poll_task is not None:
            self._reminder_poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reminder_poll_task
            self._reminder_poll_task = None
        if self._startup_changelog_task is not None:
            self._startup_changelog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_changelog_task
            self._startup_changelog_task = None
        if self._daily_log_summary_task is not None:
            self._daily_log_summary_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._daily_log_summary_task
            self._daily_log_summary_task = None
        telemetry_writer = getattr(self._chat_orchestrator, "telemetry_writer", None)
        if telemetry_writer is not None:
            await telemetry_writer.close()
        await super().close()

    async def _post_startup_changelog(self) -> None:
        await self._changelog_service.post_startup()

    async def _post_changelog_announcement(self, channel_id: int, content: str) -> bool:
        return await self._changelog_service.post_announcement(channel_id, content)

    async def _get_last_changelog_snapshot(self, session, *, guild_id: int) -> str | None:
        return await self._changelog_service.get_last_snapshot(session, guild_id=guild_id)

    async def _get_changelog_channel_id(self, session, *, guild_id: int) -> int | None:
        return await self._changelog_service.get_channel_id(session, guild_id=guild_id)

    async def _set_changelog_channel_id(self, session, *, guild_id: int, channel_id: int | None) -> None:
        await self._changelog_service.set_channel_id(
            session,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def _run_reminder_poll_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._dispatch_due_reminders()
                await self._run_retention_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive path
                LOGGER.exception("Reminder polling failed.")
            await asyncio.sleep(self.settings.reminder_poll_seconds)

    async def _run_daily_log_summary_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._post_daily_log_summary_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive path
                LOGGER.exception("Daily debug log summary failed.")
            await asyncio.sleep(DAILY_LOG_SUMMARY_CHECK_SECONDS)

    async def _post_daily_log_summary_if_due(self) -> None:
        await post_daily_logs_summary_if_due(self, database=self.database, settings=self.settings)

    async def _dispatch_due_reminders(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.database.session() as session:
            due_reminders = await self.reminder_service.list_due_reminders(session, due_before=now)
            if not due_reminders:
                return
            delivered_any = False
            for reminder in due_reminders:
                if await self._deliver_reminder(reminder):
                    await self.reminder_service.mark_delivered(session, reminder, delivered_at=datetime.now(timezone.utc))
                    delivered_any = True
            if delivered_any:
                await session.commit()

    async def _run_retention_maintenance(self, *, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if not force and self._last_retention_run_at is not None:
            elapsed_seconds = (now - self._last_retention_run_at).total_seconds()
            if elapsed_seconds < RETENTION_CHECK_INTERVAL_SECONDS:
                return
        usage_cutoff = now - timedelta(days=USAGE_EVENTS_RETENTION_DAYS)
        reminder_cutoff = now - timedelta(days=DELIVERED_REMINDER_RETENTION_DAYS)
        async with self.database.session() as session:
            usage_deleted_count = await prune_usage_events_before(session, cutoff=usage_cutoff)
            message_debug_deleted_count = await prune_message_debug_events_before(
                session,
                cutoff=usage_cutoff,
            )
            agent_telemetry_deleted_count = await prune_agent_telemetry_before(
                session,
                cutoff=usage_cutoff,
            )
            action_state_deleted_count = await prune_action_idempotency_before(
                session,
                cutoff=usage_cutoff,
            )
            reminder_deleted_count = await self.reminder_service.prune_delivered_before(
                session,
                cutoff=reminder_cutoff,
            )
            memory_deleted_count = await self.memory_service.prune_stale_memories(
                session,
                now=now,
                never_retrieved_older_than_days=MEMORY_RETENTION_NEVER_RETRIEVED_DAYS,
                stale_retrieved_older_than_days=MEMORY_RETENTION_STALE_RETRIEVED_DAYS,
            )
            if (
                usage_deleted_count > 0
                or message_debug_deleted_count > 0
                or agent_telemetry_deleted_count > 0
                or action_state_deleted_count > 0
                or reminder_deleted_count > 0
                or memory_deleted_count > 0
            ):
                await session.commit()
            if usage_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s usage event(s) older than %s days.",
                    usage_deleted_count,
                    USAGE_EVENTS_RETENTION_DAYS,
                )
            if message_debug_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s message debug event(s) older than %s days.",
                    message_debug_deleted_count,
                    USAGE_EVENTS_RETENTION_DAYS,
                )
            if agent_telemetry_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s agent run/step/tool telemetry event(s) older than %s days.",
                    agent_telemetry_deleted_count,
                    USAGE_EVENTS_RETENTION_DAYS,
                )
            if action_state_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s expired action idempotency key(s).",
                    action_state_deleted_count,
                )
            if reminder_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s delivered reminder(s) older than %s days.",
                    reminder_deleted_count,
                    DELIVERED_REMINDER_RETENTION_DAYS,
                )
            if memory_deleted_count > 0:
                LOGGER.info(
                    "Pruned %s stale memory row(s) (never retrieved > %s days, or last retrieved > %s days).",
                    memory_deleted_count,
                    MEMORY_RETENTION_NEVER_RETRIEVED_DAYS,
                    MEMORY_RETENTION_STALE_RETRIEVED_DAYS,
                )
        self._last_retention_run_at = now

    async def _record_message_debug_stats(
        self,
        *,
        metrics: dict[str, int | str],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        source_message_id: int | None,
    ) -> None:
        try:
            async with self.database.session() as session:
                await record_message_debug_stats(
                    session,
                    metrics=metrics,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    source_message_id=source_message_id,
                )
                await session.commit()
        except Exception:
            LOGGER.warning("Failed to record message debug timing stats.", exc_info=True)

    async def _deliver_reminder(self, reminder) -> bool:
        channel = self.get_channel(reminder.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(reminder.channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                LOGGER.warning("Failed to fetch reminder channel %s for reminder %s.", reminder.channel_id, reminder.id)
                return False
        jump_link = None
        if reminder.source_message_id is not None:
            jump_link = format_discord_message_link(
                guild_id=reminder.guild_id,
                channel_id=reminder.channel_id,
                message_id=reminder.source_message_id,
            )
        lines = [
            f"<@{reminder.user_id}> reminder: {reminder.reminder_text}",
            f"Scheduled for <t:{int(reminder.remind_at.timestamp())}:F>.",
        ]
        if jump_link is not None:
            lines.append(f"Original message: {jump_link}")
        try:
            await channel.send("\n".join(lines))
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed to send reminder %s into channel %s.", reminder.id, reminder.channel_id)
            return False
        return True

    async def on_message(self, message: discord.Message) -> None:
        request_started_at = time.perf_counter()
        if message.author.bot or message.guild is None or self.user is None:
            return
        if not is_configured_guild(
            guild_id=message.guild.id,
            configured_guild_id=self.settings.discord_guild_id,
        ):
            return
        if is_bad_bot_feedback(message.content):
            if await self._handle_bad_bot_feedback(message):
                return
        if not await self._should_trigger_on_message(message):
            return

        cleaned_prompt = clean_trigger_content(
            message,
            bot_user_id=self.user.id if self.user is not None else None,
        )
        effective_prompt = cleaned_prompt or "Reply naturally to the conversation above."
        if is_plsfix_request(effective_prompt):
            await self._handle_plsfix_request(message, effective_prompt)
            return

        request_key = (message.channel.id, message.author.id)
        if self._active_requests.has_active(request_key):
            await message.reply(
                "You already have an active request in this channel. Use `/cancel` to stop it.",
                mention_author=False,
            )
            return

        typing_done = asyncio.Event()
        await _try_send_typing_once(message.channel)
        typing_task = asyncio.create_task(
            _send_typing_while_pending(message.channel, typing_done, send_initial=False)
        )
        progress_task: asyncio.Task[discord.Message | None] | None = asyncio.create_task(
            _send_delayed_progress(message)
        )
        task: asyncio.Task[tuple[str, dict[str, int | str] | None]] | None = None
        try:
            context_started_at = time.perf_counter()
            context_lines, context_image_urls, image_context_lines = await self._message_context_collector.build_message_context(
                message,
            )
            context_fetch_ms = elapsed_ms(context_started_at)
            latency_debug_enabled = message.author.id in self._latency_debug_enabled_users
            memory_debug_enabled = message.author.id in self._memory_debug_enabled_users
            show_think_enabled = message.author.id in self._thinking_enabled_users
            task = self._active_requests.start(
                request_key,
                self._generate_reply(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    user_name=message.author.display_name,
                    user_global_name=message.author.global_name or message.author.name,
                    mentioned_user_ids=[user.id for user in message.mentions],
                    prompt=effective_prompt,
                    context_lines=context_lines,
                    image_attachment_urls=context_image_urls,
                    image_context_lines=image_context_lines,
                    source_message_id=message.id,
                    request_started_at=request_started_at,
                    depth_override=self._depth_preferences.get(message.author.id),
                    collect_latency_debug=True,
                    collect_memory_debug=memory_debug_enabled,
                    show_think_enabled=show_think_enabled,
                ),
            )
            try:
                reply, metrics = await task
            except asyncio.CancelledError:
                progress_message = await _claim_delayed_progress(progress_task)
                progress_task = None
                await _edit_progress_or_reply(
                    message,
                    progress_message,
                    "Cancelled your active request.",
                )
                return
            except Exception as exc:
                LOGGER.exception(
                    "Reply generation failed for message %s in channel %s.",
                    message.id,
                    message.channel.id,
                )
                await send_reply_generation_error_debug(
                    self,
                    channel_id=self.settings.error_debug_channel_id,
                    message=message,
                    exc=exc,
                )
                progress_message = await _claim_delayed_progress(progress_task)
                progress_task = None
                with suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                    await _edit_progress_or_reply(
                        message,
                        progress_message,
                        "I hit an upstream model/provider error for that request. Please retry in a moment.",
                    )
                return
            finally:
                self._active_requests.clear(request_key, task)
            if latency_debug_enabled and metrics is not None:
                metrics["context_fetch_ms"] = context_fetch_ms
                metrics["end_to_end_ms"] = elapsed_ms(request_started_at)
                reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
            reply = self._render_discord_emojis(reply, message.guild)
            send_started_at = time.perf_counter()
            progress_message = await _claim_delayed_progress(progress_task)
            progress_task = None
            sent_messages = await self._send_message_reply_chunks(
                message,
                reply,
                progress_message=progress_message,
            )
            if metrics is not None:
                metrics["reply_send_ms"] = elapsed_ms(send_started_at)
                metrics["context_fetch_ms"] = context_fetch_ms
                metrics["end_to_end_ms"] = elapsed_ms(request_started_at)
                bot_message_ids = [
                    sent.id
                    for sent in sent_messages
                    if getattr(sent, "id", None) is not None
                ]
                if bot_message_ids:
                    self._response_diagnostic_cache.record(
                        ResponseDiagnosticSnapshot(
                            captured_at=datetime.now(timezone.utc),
                            guild_id=message.guild.id,
                            channel_id=message.channel.id,
                            source_message_id=message.id,
                            source_message_url=message.jump_url,
                            source_user_id=message.author.id,
                            prompt=effective_prompt,
                            context_lines=tuple(context_lines),
                            image_context_lines=tuple(image_context_lines),
                            reply_text=reply,
                            metrics=dict(metrics),
                        ),
                        bot_message_ids=bot_message_ids,
                    )
                await self._record_message_debug_stats(
                    metrics=metrics,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    source_message_id=message.id,
                )
                await send_provider_recovery_debug(
                    self,
                    channel_id=self.settings.error_debug_channel_id,
                    message=message,
                    metrics=metrics,
                )
                await send_finalization_failure_debug(
                    self,
                    channel_id=self.settings.error_debug_channel_id,
                    message=message,
                    metrics=metrics,
                )
        finally:
            if progress_task is not None:
                progress_message = await _claim_delayed_progress(progress_task)
                if progress_message is not None:
                    with suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                        await progress_message.delete()
            typing_done.set()
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    async def _handle_bad_bot_feedback(self, message: discord.Message) -> bool:
        reference_message_id = getattr(
            getattr(message, "reference", None),
            "message_id",
            None,
        )
        snapshot = self._response_diagnostic_cache.find(
            channel_id=message.channel.id,
            reference_message_id=reference_message_id,
            now=datetime.now(timezone.utc),
        )
        if snapshot is None:
            return False
        sent = await send_bad_bot_feedback(
            self,
            database=self.database,
            debug_channel_id=self.settings.error_debug_channel_id,
            snapshot=snapshot,
            feedback_message=message,
        )
        if sent:
            await message.reply("Logged that response for review.", mention_author=False)
        else:
            await message.reply(
                "I couldn't log that response because the debug channel is not configured.",
                mention_author=False,
            )
        return True

    async def _handle_plsfix_request(self, message: discord.Message, prompt: str) -> None:
        admin_user_id = self.settings.discord_admin_user_id
        if admin_user_id is None:
            await message.reply(
                "`plsfix` is disabled until `DISCORD_ADMIN_USER_ID` is configured.",
                mention_author=False,
            )
            return
        if message.author.id != admin_user_id:
            await message.reply(
                "`plsfix` is admin-only because it captures operational telemetry.",
                mention_author=False,
            )
            return
        if self.settings.error_debug_channel_id is None:
            await message.reply(
                "`ERROR_DEBUG_CHANNEL_ID` is not configured, so I have nowhere to post the bundle.",
                mention_author=False,
            )
            return
        sent = await send_plsfix_diagnostics(
            self,
            database=self.database,
            settings=self.settings,
            request=DiagnosticRequest(
                guild_id=message.guild.id if message.guild is not None else None,
                channel_id=message.channel.id,
                user_id=message.author.id,
                message_id=message.id,
                message_url=message.jump_url,
                prompt=prompt,
            ),
        )
        if sent:
            await message.reply(
                "Captured a `plsfix` diagnostics bundle in the debug channel.",
                mention_author=False,
            )
        else:
            await message.reply("I couldn't capture a `plsfix` diagnostics bundle.", mention_author=False)

    async def _should_trigger_on_message(self, message: discord.Message) -> bool:
        if self.user is None:
            return False
        if self.user.mentioned_in(message):
            return True
        if message.reference is None or message.reference.message_id is None:
            return False
        referenced = message.reference.resolved
        if isinstance(referenced, discord.Message):
            return bool(referenced.author and referenced.author.id == self.user.id)
        try:
            original = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        return original.author.id == self.user.id

    def _register_commands(self) -> None:
        guild = discord.Object(id=self.settings.discord_guild_id) if self.settings.discord_guild_id else None
        register_bot_commands(self, guild=guild)

    async def _generate_reply(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        user_name: str,
        user_global_name: str,
        prompt: str,
        context_lines: list[str],
        image_attachment_urls: list[str],
        image_context_lines: list[str],
        source_message_id: int | None,
        request_started_at: float | None = None,
        depth_override: AnswerProfile | str | None = None,
        mentioned_user_ids: list[int] | None = None,
        collect_latency_debug: bool = False,
        collect_memory_debug: bool = False,
        show_think_enabled: bool = False,
        include_memories: bool = True,
        tool_runner: ToolRunner | None = None,
    ) -> tuple[str, dict[str, int | str] | None]:
        reply_started_at = time.perf_counter()
        metrics: dict[str, int | str] | None = {} if collect_latency_debug else None
        selected_chat_model = self.settings.openai_chat_model
        vision_context_block = NO_IMAGE_ANALYSIS
        vision_task: asyncio.Task | None = None
        use_direct_chat_vision = should_include_images_in_chat_request(
            image_attachment_urls,
            vision_model=self.settings.openai_vision_model,
            vision_context_block=vision_context_block,
            chat_model=selected_chat_model,
        )
        if (
            image_attachment_urls
            and self.settings.openai_vision_model
            and not use_direct_chat_vision
        ):
            vision_task = asyncio.create_task(
                self._vision_context_service.build_context(
                    prompt=prompt,
                    image_attachment_urls=image_attachment_urls,
                    image_context_lines=image_context_lines,
                )
            )
        if metrics is not None:
            metrics["chat_model"] = self.settings.openai_chat_model
            metrics["memory_model"] = self.settings.openai_memory_model
            metrics["vision_model"] = self.settings.openai_vision_model or "(none)"
            metrics["active_chat_model"] = selected_chat_model
            metrics["image_attachment_count"] = len(image_attachment_urls)
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        image_context_block = "\n".join(image_context_lines) or "(no included images)"
        async with self.database.session() as session:
            prepared_context = await self._chat_context_builder.prepare(
                session,
                guild_id=guild_id,
                user_id=user_id,
                prompt=prompt,
                context_text=context_block,
                include_memories=include_memories,
                mentioned_user_ids=mentioned_user_ids or [],
                now=datetime.now(timezone.utc),
            )
            commit_started_at = time.perf_counter()
            await session.commit()
            if metrics is not None:
                metrics["memory_retrieval_ms"] = prepared_context.memory_retrieval_ms
                metrics["chat_commit_ms"] = elapsed_ms(commit_started_at)
        if vision_task is not None:
            vision_result = await vision_task
            vision_context_block = vision_result.text
            if vision_result.usage is not None:
                async with self.database.session() as session:
                    await record_usage(
                        session,
                        usage=vision_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    await session.commit()
            if metrics is not None and vision_result.elapsed_ms > 0:
                metrics["vision_summary_ms"] = vision_result.elapsed_ms
        user_prompt_text = build_user_prompt(
            user_name=user_name,
            user_id=user_id,
            user_global_name=user_global_name,
            owner_context=self._owner_context(user_id),
            current_datetime_text=prepared_context.current_datetime_text,
            prompt=prompt,
            context_block=context_block,
            extended_context_block=f"(not requested yet; use `{GET_CHANNEL_CONTEXT_TOOL_NAME}` if older Discord context is needed)",
            image_context_block=image_context_block,
            vision_context_block=vision_context_block,
            personal_profile_block=prepared_context.personal_profile_block,
            memories_block=prepared_context.memories_block,
            channel_alias_block=prepared_context.channel_alias_block,
            member_alias_block=prepared_context.member_alias_block,
            mentioned_user_memories_block=prepared_context.mentioned_user_memories_block,
        )
        use_chat_model_image_input = should_include_images_in_chat_request(
            image_attachment_urls,
            vision_model=self.settings.openai_vision_model,
            vision_context_block=vision_context_block,
            chat_model=selected_chat_model,
        )
        chat_image_inputs = (
            await self._vision_context_service.prepare_image_inputs_for_model(
                model=selected_chat_model,
                image_urls=image_attachment_urls,
            )
            if use_chat_model_image_input
            else []
        )
        message_content = (
            build_multimodal_user_content(user_prompt_text, chat_image_inputs)
            if use_chat_model_image_input
            else user_prompt_text
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self._build_system_prompt()},
            {
                "role": "user",
                "content": message_content,
            },
        ]
        text, reasoning_parts = await self._chat_orchestrator.run_chat_with_tools(
            chat_model=selected_chat_model,
            messages=messages,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            request_text=prompt,
            metrics=metrics,
            tool_runner=tool_runner,
            depth_override=depth_override,
            request_started_at=request_started_at,
        )
        self._schedule_memory_extraction(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            current_message=prompt,
            recent_context=context_block,
        )
        text = strip_think_blocks(text)
        if not text:
            text = "I didn't get enough signal there. Try asking again with a little more detail."
        if show_think_enabled and reasoning_parts:
            thinking_block = format_thinking_block(reasoning_parts)
            if thinking_block:
                text = append_debug_block(text, thinking_block, limit=None)
        if collect_memory_debug:
            text = append_debug_block(
                text,
                format_memory_debug_block(
                    memory_enabled=prepared_context.memory_enabled,
                    memory_retrieval_ms=prepared_context.memory_retrieval_ms,
                    embedding_model=self.settings.openai_embedding_model,
                    embedding_api_key_mode=(
                        "separate-configured"
                        if self.settings.openai_embedding_api_key
                        else "inherits-openai-api-key"
                    ),
                    embedding_base_url_mode=(
                        "separate-configured"
                        if self.settings.openai_embedding_base_url
                        else (
                            "openai-default"
                            if self.settings.openai_embedding_api_key or not self.settings.openai_base_url
                            else "shared-openai-base-url"
                        )
                    ),
                    memories=prepared_context.retrieved_memories,
                ),
                limit=None,
        )
        if metrics is not None:
            metrics["reply_generation_ms"] = elapsed_ms(reply_started_at)
        return text, metrics

    def _schedule_memory_extraction(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> None:
        self._background_memory_writer.schedule(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            current_message=current_message,
            recent_context=recent_context,
        )

    def _build_system_prompt(self) -> str:
        return get_system_prompt()

    def _owner_context(self, user_id: int) -> str:
        owner_id = self.settings.discord_admin_user_id
        if owner_id is None:
            return "No owner/admin user ID is configured."
        if user_id == owner_id:
            return f"Current user is the configured bot owner/admin (Discord user ID {owner_id})."
        return f"Configured bot owner/admin Discord user ID: {owner_id}. Current user is not the owner/admin."

    def _render_discord_emojis(self, text: str, guild: discord.Guild | None) -> str:
        if guild is None:
            return text
        replacements: dict[str, str] = {}
        for alias in ALLOWED_CUSTOM_EMOJI_ALIASES:
            emoji = discord.utils.get(guild.emojis, name=alias)
            if emoji is None:
                continue
            replacements[alias] = str(emoji)
        return render_custom_emoji_aliases(text, replacements)

    async def _send_message_reply_chunks(
        self,
        message: discord.Message,
        text: str,
        *,
        progress_message: discord.Message | None = None,
    ) -> list[discord.Message]:
        text = normalize_discord_math(text)
        table_extraction = extract_markdown_tables_as_images(text)
        text = table_extraction.text or text
        if not table_extraction.images:
            text = normalize_discord_tables(text)
        chunks = split_message_chunks(text)
        files = [
            discord.File(BytesIO(image.data), filename=image.filename)
            for image in table_extraction.images
        ]
        if progress_message is not None and files:
            with suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                await progress_message.delete()
            progress_message = None
        if not chunks:
            if progress_message is not None:
                try:
                    return [await progress_message.edit(content=text)]
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass
            sent = await message.reply(text, mention_author=False, files=files)
            return [sent]
        if progress_message is not None:
            try:
                sent_messages = [await progress_message.edit(content=chunks[0])]
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                sent_messages = [
                    await message.reply(chunks[0], mention_author=False, files=files)
                ]
        else:
            sent_messages = [
                await message.reply(chunks[0], mention_author=False, files=files)
            ]
        for chunk in chunks[1:]:
            sent_messages.append(await message.channel.send(chunk))
        return sent_messages

    async def _send_interaction_reply_chunks(
        self,
        interaction: discord.Interaction,
        text: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        chunks = split_message_chunks(text)
        if not chunks:
            await interaction.followup.send(text, ephemeral=ephemeral)
            return
        await interaction.followup.send(chunks[0], ephemeral=ephemeral)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

async def _try_send_typing_once(channel: object) -> None:
    trigger_typing = getattr(channel, "trigger_typing", None)
    try:
        if trigger_typing is not None:
            await trigger_typing()
            return
        typing = getattr(channel, "typing", None)
        if typing is None:
            return
        async with typing():
            return
    except Exception:
        LOGGER.debug("Discord typing indicator failed; continuing without it.", exc_info=True)


async def _send_delayed_progress(
    message: discord.Message,
    *,
    delay_seconds: float = PROGRESS_MESSAGE_DELAY_SECONDS,
) -> discord.Message | None:
    try:
        await asyncio.sleep(max(delay_seconds, 0.0))
        return await message.reply(PROGRESS_MESSAGE_TEXT, mention_author=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.debug("Discord progress message failed; continuing with typing only.", exc_info=True)
        return None


async def _claim_delayed_progress(
    task: asyncio.Task[discord.Message | None],
) -> discord.Message | None:
    if not task.done():
        task.cancel()
    try:
        return await task
    except asyncio.CancelledError:
        return None
    except Exception:
        LOGGER.debug("Discord progress task failed; continuing without it.", exc_info=True)
        return None


async def _edit_progress_or_reply(
    source_message: discord.Message,
    progress_message: discord.Message | None,
    content: str,
) -> discord.Message:
    if progress_message is not None:
        try:
            return await progress_message.edit(content=content)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
    return await source_message.reply(content, mention_author=False)


async def _send_typing_while_pending(channel: object, done: asyncio.Event, *, send_initial: bool = True) -> None:
    if send_initial:
        await _try_send_typing_once(channel)
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=TYPING_HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            await _try_send_typing_once(channel)
            continue
        except asyncio.CancelledError:
            raise
