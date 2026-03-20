from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from nycti.channel_aliases import ChannelAliasService, normalize_channel_alias
from nycti.changelog import build_changelog_announcement
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.config import Settings
from nycti.db.models import AppState
from nycti.db.session import Database
from nycti.discord.help import register_help_command
from nycti.formatting import (
    append_debug_block,
    extract_search_query,
    format_channel_alias_list,
    format_discord_message_link,
    format_current_datetime_context,
    format_latency_debug_block,
    format_ping_message,
    format_reminder_list,
    format_thinking_block,
    normalize_discord_tables,
    render_custom_emoji_aliases,
    split_message_chunks,
    strip_think_blocks,
)
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.prompts import get_system_prompt
from nycti.request_control import ActiveRequestRegistry
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.timezones import canonicalize_timezone_name
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
ALLOWED_CUSTOM_EMOJI_ALIASES = ("pepebeat", "pepeww", "kekw", "javsigh")


class NyctiBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        llm_client: OpenAIClient,
        tavily_client: TavilyClient,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.database = database
        self.llm_client = llm_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self._active_requests = ActiveRequestRegistry()
        self._chat_orchestrator = ChatOrchestrator(
            settings=settings,
            llm_client=llm_client,
            tavily_client=tavily_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            reminder_service=reminder_service,
            bot=self,
        )
        self._latency_debug_enabled_users: set[int] = set()
        self._thinking_enabled_users: set[int] = set()
        self._reminder_poll_task: asyncio.Task[None] | None = None
        self._startup_changelog_task: asyncio.Task[None] | None = None
        self._register_commands()

    async def setup_hook(self) -> None:
        await self.database.init_models()
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if self._reminder_poll_task is None:
            self._reminder_poll_task = asyncio.create_task(self._run_reminder_poll_loop())
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
        await super().close()

    async def _post_startup_changelog(self) -> None:
        await self.wait_until_ready()

        async with self.database.session() as session:
            channel_ids = await self._list_configured_changelog_channels(session)
            if not channel_ids:
                return
            posted_any = False
            for guild_id, channel_id in channel_ids:
                previous_snapshot = await self._get_last_changelog_snapshot(session, guild_id=guild_id)
                announcement = build_changelog_announcement(
                    self.settings,
                    previous_snapshot=previous_snapshot,
                )
                if announcement is None:
                    continue
                sent = await self._post_changelog_announcement(channel_id, announcement.content)
                if not sent:
                    continue
                await self._set_last_changelog_snapshot(session, guild_id=guild_id, snapshot=announcement.snapshot)
                posted_any = True
            if posted_any:
                await session.commit()

    async def _post_changelog_announcement(self, channel_id: int, content: str) -> bool:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                LOGGER.warning("Failed to fetch changelog channel %s.", channel_id)
                return False
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed to post changelog into channel %s.", channel_id)
            return False
        return True

    async def _get_last_changelog_snapshot(self, session, *, guild_id: int) -> str | None:
        state = await session.get(AppState, self._changelog_snapshot_key(guild_id))
        if state is None:
            return None
        return state.value

    async def _set_last_changelog_snapshot(self, session, *, guild_id: int, snapshot: str) -> None:
        state = await session.get(AppState, self._changelog_snapshot_key(guild_id))
        if state is None:
            session.add(AppState(key=self._changelog_snapshot_key(guild_id), value=snapshot))
            await session.flush()
            return
        state.value = snapshot
        await session.flush()

    async def _list_configured_changelog_channels(self, session) -> list[tuple[int, int]]:
        stmt = select(AppState).where(AppState.key.like("changelog_channel_id:%"))
        states = list((await session.scalars(stmt)).all())
        configured: list[tuple[int, int]] = []
        for state in states:
            try:
                guild_id = int(state.key.split(":", 1)[1])
                channel_id = int(state.value)
            except (IndexError, ValueError):
                continue
            configured.append((guild_id, channel_id))
        return configured

    async def _get_changelog_channel_id(self, session, *, guild_id: int) -> int | None:
        state = await session.get(AppState, self._changelog_channel_key(guild_id))
        if state is None:
            return None
        try:
            return int(state.value)
        except ValueError:
            return None

    async def _set_changelog_channel_id(self, session, *, guild_id: int, channel_id: int | None) -> None:
        key = self._changelog_channel_key(guild_id)
        state = await session.get(AppState, key)
        if channel_id is None:
            if state is not None:
                await session.delete(state)
                await session.flush()
            return
        if state is None:
            session.add(AppState(key=key, value=str(channel_id)))
            await session.flush()
            return
        state.value = str(channel_id)
        await session.flush()

    @staticmethod
    def _changelog_channel_key(guild_id: int) -> str:
        return f"changelog_channel_id:{guild_id}"

    @staticmethod
    def _changelog_snapshot_key(guild_id: int) -> str:
        return f"last_changelog_snapshot:{guild_id}"

    async def _run_reminder_poll_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._dispatch_due_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive path
                LOGGER.exception("Reminder polling failed.")
            await asyncio.sleep(self.settings.reminder_poll_seconds)

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
        if message.author.bot or message.guild is None or self.user is None:
            return
        if not await self._should_trigger_on_message(message):
            return

        request_key = (message.channel.id, message.author.id)
        if self._active_requests.has_active(request_key):
            await message.reply(
                "You already have an active request in this channel. Use `/cancel_all` to cancel active prompts.",
                mention_author=False,
            )
            return

        cleaned_prompt = self._clean_trigger_content(message)
        effective_prompt = cleaned_prompt or "Reply naturally to the conversation above."
        search_requested, effective_prompt = extract_search_query(effective_prompt)
        if not effective_prompt:
            effective_prompt = "Reply naturally to the conversation above."
        request_started_at = time.perf_counter()
        context_started_at = time.perf_counter()
        context_lines = await self._fetch_context_lines(
            message.channel,
            before=message,
            include_current=message,
        )
        context_fetch_ms = self._elapsed_ms(context_started_at)
        latency_debug_enabled = message.author.id in self._latency_debug_enabled_users
        show_think_enabled = message.author.id in self._thinking_enabled_users
        task = self._active_requests.start(
            request_key,
            self._generate_reply(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                user_global_name=message.author.global_name or message.author.name,
                prompt=effective_prompt,
                context_lines=context_lines,
                source_message_id=message.id,
                collect_latency_debug=latency_debug_enabled,
                show_think_enabled=show_think_enabled,
                search_requested=search_requested,
            ),
        )
        try:
            async with message.channel.typing():
                reply, metrics = await task
        except asyncio.CancelledError:
            await message.reply("Cancelled your active request.", mention_author=False)
            return
        finally:
            self._active_requests.clear(request_key, task)
        if latency_debug_enabled and metrics is not None:
            metrics["context_fetch_ms"] = context_fetch_ms
            metrics["end_to_end_ms"] = self._elapsed_ms(request_started_at)
            reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
        reply = self._render_discord_emojis(reply, message.guild)
        await self._send_message_reply_chunks(message, reply)

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
        register_help_command(self.tree, guild=guild)

        @self.tree.command(name="ping", description="Check whether the bot is online.", guild=guild)
        async def ping(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(format_ping_message(self.latency), ephemeral=True)

        @self.tree.command(name="reminders", description="Show your pending reminders.", guild=guild)
        async def reminders(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            async with self.database.session() as session:
                timezone_name = await self.memory_service.get_timezone_name(session, interaction.user.id)
                reminders_list = await self.reminder_service.list_pending_for_user(session, user_id=interaction.user.id)
            if not reminders_list:
                await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
                return
            await interaction.response.send_message(
                format_reminder_list(reminders_list, timezone_name=timezone_name),
                ephemeral=True,
            )

        @self.tree.command(name="reminders_all", description="Show all pending reminders in this server.", guild=guild)
        async def reminders_all(interaction: discord.Interaction) -> None:
            if interaction.user is None or interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to view all pending reminders.",
                        ephemeral=True,
                    )
                    return
            async with self.database.session() as session:
                timezone_name = await self.memory_service.get_timezone_name(session, interaction.user.id)
                reminders_list = await self.reminder_service.list_pending_for_guild(
                    session,
                    guild_id=interaction.guild.id,
                )
            if not reminders_list:
                await interaction.response.send_message("There are no pending reminders in this server.", ephemeral=True)
                return
            await interaction.response.send_message(
                format_reminder_list(reminders_list, timezone_name=timezone_name, include_owner=True),
                ephemeral=True,
            )

        @self.tree.command(name="forget_reminder", description="Delete one of your pending reminders by ID.", guild=guild)
        @app_commands.describe(reminder_id="The reminder ID shown by /reminders.")
        async def forget_reminder(interaction: discord.Interaction, reminder_id: int) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            async with self.database.session() as session:
                deleted = await self.reminder_service.delete_reminder(
                    session,
                    user_id=interaction.user.id,
                    reminder_id=reminder_id,
                )
                await session.commit()
            message = "Reminder deleted." if deleted else "No pending reminder found for that ID."
            await interaction.response.send_message(message, ephemeral=True)

        benchmark_group = app_commands.Group(name="benchmark", description="Run benchmark tasks")
        config_group = app_commands.Group(name="config", description="Configure your bot settings")
        test_group = app_commands.Group(name="test", description="Run test utilities")
        channel_group = app_commands.Group(name="channel", description="Manage cross-channel aliases")

        @benchmark_group.command(name="earnings", description="Benchmark a no-context earnings comparison.")
        async def benchmark_earnings(interaction: discord.Interaction) -> None:
            if interaction.channel is None or interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            channel_id = getattr(interaction.channel, "id", None)
            if channel_id is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            request_key = (channel_id, interaction.user.id)
            if self._active_requests.has_active(request_key):
                await interaction.response.send_message(
                    "You already have an active request in this channel. Use `/cancel_all` to cancel active prompts.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            request_started_at = time.perf_counter()
            show_think_enabled = interaction.user.id in self._thinking_enabled_users
            task = self._active_requests.start(
                request_key,
                self._generate_reply(
                    guild_id=interaction.guild.id if interaction.guild else None,
                    channel_id=channel_id,
                    user_id=interaction.user.id,
                    user_name=interaction.user.display_name,
                    user_global_name=interaction.user.global_name or interaction.user.name,
                    prompt="Compare the latest NVIDIA and AMD earnings reports. Focus on revenue, EPS, and guidance.",
                    context_lines=[],
                    source_message_id=None,
                    collect_latency_debug=True,
                    show_think_enabled=show_think_enabled,
                    search_requested=True,
                    include_memories=False,
                ),
            )
            try:
                reply, metrics = await task
            except asyncio.CancelledError:
                await interaction.followup.send("Cancelled your active request.", ephemeral=True)
                return
            finally:
                self._active_requests.clear(request_key, task)
            metrics = metrics or {}
            metrics["context_fetch_ms"] = 0
            metrics["end_to_end_ms"] = self._elapsed_ms(request_started_at)
            reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
            reply = self._render_discord_emojis(reply, interaction.guild)
            await self._send_interaction_reply_chunks(interaction, reply, ephemeral=True)

        self.tree.add_command(benchmark_group, guild=guild)

        @config_group.command(name="time", description="Set your timezone for reminders and date context.")
        @app_commands.describe(timezone="Timezone like PST or America/Los_Angeles")
        async def config_time(interaction: discord.Interaction, timezone: str) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            canonical_timezone = canonicalize_timezone_name(timezone)
            if canonical_timezone is None:
                await interaction.response.send_message(
                    "Unknown timezone. Use something like `PST`, `UTC`, or `America/Los_Angeles`.",
                    ephemeral=True,
                )
                return
            async with self.database.session() as session:
                stored_timezone = await self.memory_service.set_timezone_name(
                    session,
                    interaction.user.id,
                    canonical_timezone,
                )
                await session.commit()
            await interaction.response.send_message(
                f"Timezone set to `{stored_timezone}` for your reminders and date context.",
                ephemeral=True,
            )

        @config_group.command(name="changelog", description="Set or clear the startup changelog channel for this server.")
        @app_commands.describe(channel="Target channel; leave empty to clear the server changelog channel")
        async def config_changelog(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if interaction.user is None or interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to configure the changelog channel.",
                        ephemeral=True,
                    )
                    return
            async with self.database.session() as session:
                await self._set_changelog_channel_id(
                    session,
                    guild_id=interaction.guild.id,
                    channel_id=channel.id if channel is not None else None,
                )
                await session.commit()
            if channel is None:
                await interaction.response.send_message("Startup changelog channel cleared for this server.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Startup changelog channel set to {channel.mention}.",
                ephemeral=True,
            )

        self.tree.add_command(config_group, guild=guild)

        @self.tree.command(name="show", description="Toggle reply overlays for your replies.", guild=guild)
        @app_commands.describe(
            debug="true to include timing diagnostics, false to disable them",
            thinking="true to allow reasoning summary, false to hide it",
        )
        async def show(
            interaction: discord.Interaction,
            debug: bool | None = None,
            thinking: bool | None = None,
        ) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if debug is None and thinking is None:
                await interaction.response.send_message(
                    "Provide `debug` and/or `thinking` as true or false.",
                    ephemeral=True,
                )
                return

            messages: list[str] = []
            if debug is not None:
                if debug:
                    self._latency_debug_enabled_users.add(interaction.user.id)
                    messages.append("Latency debug enabled.")
                else:
                    self._latency_debug_enabled_users.discard(interaction.user.id)
                    messages.append("Latency debug disabled.")
            if thinking is not None:
                if thinking:
                    self._thinking_enabled_users.add(interaction.user.id)
                    messages.append("Reasoning summary enabled.")
                else:
                    self._thinking_enabled_users.discard(interaction.user.id)
                    messages.append("Reasoning summary disabled.")
            messages.append("These toggles reset on bot restart.")
            await interaction.response.send_message(" ".join(messages), ephemeral=True)

        @test_group.command(name="changelog", description="Post the current changelog message to the changelog channel.")
        async def test_changelog(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to test changelog posting.",
                        ephemeral=True,
                    )
                    return
            if interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            async with self.database.session() as session:
                channel_id = await self._get_changelog_channel_id(session, guild_id=interaction.guild.id)
                previous_snapshot = await self._get_last_changelog_snapshot(session, guild_id=interaction.guild.id)
            announcement = build_changelog_announcement(
                self.settings,
                previous_snapshot=previous_snapshot,
            )
            if announcement is None:
                await interaction.response.send_message(
                    "No new changelog lines are pending. Update `src/nycti/changelog.md`, or ensure `.git` is available for commit-message fallback.",
                    ephemeral=True,
                )
                return
            if channel_id is None:
                await interaction.response.send_message(
                    "No changelog channel is configured for this server. Use `/config changelog` first.",
                    ephemeral=True,
                )
                return
            sent = await self._post_changelog_announcement(channel_id, announcement.content)
            if not sent:
                await interaction.response.send_message("Failed to post the changelog test message.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Posted changelog test message to <#{channel_id}>.",
                ephemeral=True,
            )

        self.tree.add_command(test_group, guild=guild)

        @self.tree.command(name="cancel_all", description="Cancel all active in-flight prompts.", guild=guild)
        async def cancel_all(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to cancel all active prompts.",
                        ephemeral=True,
                    )
                    return
            cancelled_count = self._active_requests.cancel_all()
            if cancelled_count == 0:
                await interaction.response.send_message("No active prompts to cancel.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Cancelling `{cancelled_count}` active prompt(s).",
                ephemeral=True,
            )

        @self.tree.command(name="reset", description="Hard reset runtime state for the bot.", guild=guild)
        async def reset(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to reset bot runtime state.",
                        ephemeral=True,
                    )
                    return
            cancelled_count = self._active_requests.cancel_all()
            self._latency_debug_enabled_users.clear()
            self._thinking_enabled_users.clear()
            get_system_prompt.cache_clear()
            await interaction.response.send_message(
                (
                    "Runtime reset complete. "
                    f"Cancelled `{cancelled_count}` active prompt(s), cleared debug/thinking toggles, "
                    "and refreshed the cached system prompt for the next request."
                ),
                ephemeral=True,
            )

        @self.tree.command(name="memories", description="Show your stored memories.", guild=guild)
        async def memories(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                memories_list = await self.memory_service.list_memories(session, interaction.user.id, limit=10)
                await interaction.response.send_message(
                    self.memory_service.format_memory_list(memories_list),
                    ephemeral=True,
                )

        @self.tree.command(name="forget", description="Delete one of your stored memories by ID.", guild=guild)
        @app_commands.describe(memory_id="The memory ID shown by /memories.")
        async def forget(interaction: discord.Interaction, memory_id: int) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                deleted = await self.memory_service.delete_memory(session, interaction.user.id, memory_id)
                await session.commit()
            message = "Memory deleted." if deleted else "No memory found for that ID."
            await interaction.response.send_message(message, ephemeral=True)

        @self.tree.command(name="memory", description="Enable or disable memory retrieval and storage for you.", guild=guild)
        @app_commands.describe(enabled="true to enable memory, false to disable it")
        async def memory(interaction: discord.Interaction, enabled: bool) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                await self.memory_service.set_enabled(session, interaction.user.id, enabled)
                await session.commit()
            if enabled:
                await interaction.response.send_message("Memory enabled for your future chats.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Memory disabled. Existing memories are kept until you delete them.",
                ephemeral=True,
            )

        @channel_group.command(name="set", description="Set or update a channel alias.")
        @app_commands.describe(alias="Short alias like alerts", channel_id="Target Discord channel ID")
        async def channel_set(interaction: discord.Interaction, alias: str, channel_id: str) -> None:
            if interaction.user is None or interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to manage channel aliases.",
                        ephemeral=True,
                    )
                    return
            normalized_alias = normalize_channel_alias(alias)
            if normalized_alias is None or not channel_id.isdigit():
                await interaction.response.send_message(
                    "Alias must use letters, numbers, `-`, or `_`, and channel_id must be numeric.",
                    ephemeral=True,
                )
                return
            async with self.database.session() as session:
                alias_row = await self.channel_alias_service.set_alias(
                    session,
                    guild_id=interaction.guild.id,
                    alias=normalized_alias,
                    channel_id=int(channel_id),
                )
                await session.commit()
            await interaction.response.send_message(
                f"Alias `{alias_row.alias}` now points to <#{alias_row.channel_id}>.",
                ephemeral=True,
            )

        @channel_group.command(name="delete", description="Delete a channel alias.")
        @app_commands.describe(alias="Alias to remove")
        async def channel_delete(interaction: discord.Interaction, alias: str) -> None:
            if interaction.user is None or interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if isinstance(interaction.user, discord.Member):
                if not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message(
                        "You need `Manage Server` permission to manage channel aliases.",
                        ephemeral=True,
                    )
                    return
            async with self.database.session() as session:
                deleted = await self.channel_alias_service.delete_alias(
                    session,
                    guild_id=interaction.guild.id,
                    alias=alias,
                )
                await session.commit()
            message = "Channel alias deleted." if deleted else "No channel alias found for that name."
            await interaction.response.send_message(message, ephemeral=True)

        @channel_group.command(name="list", description="List configured channel aliases.")
        async def channel_list(interaction: discord.Interaction) -> None:
            if interaction.user is None or interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            async with self.database.session() as session:
                aliases = await self.channel_alias_service.list_aliases(session, guild_id=interaction.guild.id)
            await interaction.response.send_message(format_channel_alias_list(aliases), ephemeral=True)

        self.tree.add_command(channel_group, guild=guild)

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
        source_message_id: int | None,
        collect_latency_debug: bool = False,
        show_think_enabled: bool = False,
        search_requested: bool = False,
        include_memories: bool = True,
    ) -> tuple[str, dict[str, int | str] | None]:
        reply_started_at = time.perf_counter()
        metrics: dict[str, int | str] | None = {} if collect_latency_debug else None
        if metrics is not None:
            metrics["chat_model"] = self.settings.openai_chat_model
            metrics["memory_model"] = self.settings.openai_memory_model
            metrics["web_search_requested"] = "yes" if search_requested else "no"
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        async with self.database.session() as session:
            timezone_name = await self.memory_service.get_timezone_name(session, user_id)
            current_datetime_text = format_current_datetime_context(datetime.now(timezone.utc), timezone_name)
            channel_aliases = (
                await self.channel_alias_service.list_aliases(session, guild_id=guild_id)
                if guild_id is not None
                else []
            )
            if include_memories:
                retrieve_started_at = time.perf_counter()
                memories = await self.memory_service.retrieve_relevant(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    query=prompt,
                )
                if metrics is not None:
                    metrics["memory_retrieval_ms"] = self._elapsed_ms(retrieve_started_at)
            else:
                memories = []
                if metrics is not None:
                    metrics["memory_retrieval_ms"] = 0
            messages: list[dict[str, object]] = [
                {"role": "system", "content": self._build_system_prompt()},
                {
                    "role": "user",
                    "content": self._build_user_prompt(
                        user_name=user_name,
                        user_id=user_id,
                        user_global_name=user_global_name,
                        current_datetime_text=current_datetime_text,
                        prompt=prompt,
                        context_block=context_block,
                        memories_block=self._format_memories(memories),
                        channel_alias_block=self._format_channel_aliases(channel_aliases),
                        search_requested=search_requested,
                    ),
                },
            ]
            text, reasoning_parts = await self._chat_orchestrator.run_chat_with_tools(
                session=session,
                messages=messages,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                search_requested=search_requested,
                metrics=metrics,
            )
            commit_started_at = time.perf_counter()
            await session.commit()
            if metrics is not None:
                metrics["chat_commit_ms"] = self._elapsed_ms(commit_started_at)
        self._schedule_memory_extraction(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            current_message=prompt,
            recent_context=context_block,
        )
        text = strip_think_blocks(text)
        text = normalize_discord_tables(text)
        if not text:
            text = "I didn't get enough signal there. Try asking again with a little more detail."
        if show_think_enabled and reasoning_parts:
            thinking_block = format_thinking_block(reasoning_parts)
            if thinking_block:
                text = append_debug_block(text, thinking_block, limit=None)
        if metrics is not None:
            metrics["reply_generation_ms"] = self._elapsed_ms(reply_started_at)
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
        asyncio.create_task(
            self._store_memory_background(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                current_message=current_message,
                recent_context=recent_context,
            )
        )

    async def _store_memory_background(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> None:
        try:
            async with self.database.session() as session:
                _, memory_result = await self.memory_service.maybe_store_memory(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    source_message_id=source_message_id,
                    current_message=current_message,
                    recent_context=recent_context,
                )
                if memory_result is not None:
                    await record_usage(
                        session,
                        usage=memory_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                await session.commit()
        except Exception:  # pragma: no cover - defensive path
            LOGGER.exception("Memory extraction failed.")

    async def _fetch_context_lines(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
        include_current: discord.Message | None,
    ) -> list[str]:
        history: list[discord.Message] = []
        async for item in channel.history(limit=self.settings.channel_context_limit, before=before, oldest_first=False):
            history.append(item)
        history.reverse()

        lines = [self._format_message_line(message) for message in history if self._message_has_visible_content(message)]
        if include_current is not None and self._message_has_visible_content(include_current):
            lines.append(self._format_message_line(include_current))
        return lines

    def _message_has_visible_content(self, message: discord.Message) -> bool:
        return bool(message.content.strip() or message.attachments)

    def _format_message_line(self, message: discord.Message) -> str:
        content = " ".join(message.content.split())
        if not content and message.attachments:
            content = f"[{len(message.attachments)} attachment(s)]"
        if len(content) > 400:
            content = f"{content[:397]}..."
        return f"{message.author.display_name}: {content}"

    def _clean_trigger_content(self, message: discord.Message) -> str:
        content = message.content
        if self.user is not None:
            content = re.sub(rf"<@!?{self.user.id}>", "", content)
        return " ".join(content.split()).strip()

    def _build_system_prompt(self) -> str:
        return get_system_prompt()

    def _build_user_prompt(
        self,
        *,
        user_name: str,
        user_id: int,
        user_global_name: str,
        current_datetime_text: str,
        prompt: str,
        context_block: str,
        memories_block: str,
        channel_alias_block: str,
        search_requested: bool = False,
    ) -> str:
        prompt_text = (
            f"Current user: {user_name} (id: {user_id}, global: {user_global_name})\n\n"
            f"Current local date/time:\n{current_datetime_text}\n\n"
            f"Current request:\n{prompt}\n\n"
            f"Recent channel context:\n{context_block}\n\n"
            f"Relevant long-term memories:\n{memories_block}\n\n"
            f"Known channel aliases:\n{channel_alias_block}\n\n"
        )
        prompt_text += (
            "Available tools:\n"
            "- `web_search(query)`: use for fresh public web information when it would improve the answer. Prefer one comprehensive search first. Only search again if the first results are clearly insufficient or conflicting.\n"
            "- `create_reminder(message, remind_at)`: use when the user asks to be reminded later. `remind_at` should be an ISO 8601 local date/time when possible. Date-only values are allowed and default to 09:00 local time.\n"
            "- `send_channel_message(channel, message)`: send a message into another Discord channel in this server. Use a known channel alias or numeric channel ID. Only use this when the user explicitly wants a message posted somewhere else.\n"
            "\n"
        )
        if search_requested:
            required_lines: list[str] = []
            if search_requested:
                required_lines.append("- The user included `use search`, so you must call `web_search` at least once.")
            prompt_text += "Required tool use for this request:\n" + "\n".join(required_lines) + "\n\n"
        prompt_text += (
            "Use tools when they materially help. Prefer one strong search query before trying multiple searches. You may call tools multiple times only if earlier results are insufficient. "
            "After tool results arrive, continue reasoning from those results and then answer.\n\n"
        )
        prompt_text += "Reply to the current request, not every message in the context window."
        return prompt_text

    def _format_memories(self, memories: Iterable[object]) -> str:
        rendered = []
        for memory in memories:
            rendered.append(f"- [{memory.category}] {memory.summary}")
        return "\n".join(rendered) if rendered else "(none)"

    def _format_channel_aliases(self, aliases: Iterable[object]) -> str:
        rendered = [f"- {alias.alias}: channel_id={alias.channel_id}" for alias in aliases]
        return "\n".join(rendered) if rendered else "(none configured)"

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

    async def _send_message_reply_chunks(self, message: discord.Message, text: str) -> None:
        chunks = split_message_chunks(text)
        if not chunks:
            await message.reply(text, mention_author=False)
            return
        await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)

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

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round(max(time.perf_counter() - started_at, 0.0) * 1000)
