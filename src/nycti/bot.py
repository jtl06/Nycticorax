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
from nycti.config import Settings
from nycti.db.models import AppState
from nycti.db.session import Database
from nycti.formatting import (
    append_debug_block,
    extract_search_query,
    extract_think_content,
    format_channel_alias_list,
    format_discord_message_link,
    format_current_datetime_context,
    format_help_message,
    format_latency_debug_block,
    format_ping_message,
    format_reminder_list,
    format_thinking_block,
    normalize_discord_tables,
    parse_json_object_payload,
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
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
from nycti.timezones import canonicalize_timezone_name, get_timezone
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
ALLOWED_CUSTOM_EMOJI_ALIASES = ("pepebeat", "pepeww", "kekw", "javsigh")
MAX_CHAT_TOOL_ITERATIONS = 4


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
        announcement = build_changelog_announcement(self.settings)
        if announcement is None:
            return

        async with self.database.session() as session:
            channel_ids = await self._list_configured_changelog_channels(session)
            if not channel_ids:
                return
            posted_any = False
            for guild_id, channel_id in channel_ids:
                if await self._is_changelog_already_posted(session, guild_id=guild_id, fingerprint=announcement.fingerprint):
                    continue
                sent = await self._post_changelog_announcement(channel_id, announcement.content)
                if not sent:
                    continue
                await self._mark_changelog_posted(session, guild_id=guild_id, fingerprint=announcement.fingerprint)
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

    async def _is_changelog_already_posted(self, session, *, guild_id: int, fingerprint: str) -> bool:
        state = await session.get(AppState, self._changelog_fingerprint_key(guild_id))
        return bool(state is not None and state.value == fingerprint)

    async def _mark_changelog_posted(self, session, *, guild_id: int, fingerprint: str) -> None:
        state = await session.get(AppState, self._changelog_fingerprint_key(guild_id))
        if state is None:
            session.add(AppState(key=self._changelog_fingerprint_key(guild_id), value=fingerprint))
            await session.flush()
            return
        state.value = fingerprint
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
    def _changelog_fingerprint_key(guild_id: int) -> str:
        return f"last_changelog_fingerprint:{guild_id}"

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

        @self.tree.command(name="chat", description="Talk to the bot in the current channel.", guild=guild)
        @app_commands.describe(prompt="What you want the bot to respond to.")
        async def chat(interaction: discord.Interaction, prompt: str) -> None:
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
            await interaction.response.defer(thinking=True)
            request_started_at = time.perf_counter()
            context_started_at = time.perf_counter()
            context_lines = await self._fetch_context_lines(interaction.channel, before=None, include_current=None)
            context_fetch_ms = self._elapsed_ms(context_started_at)
            effective_prompt = prompt
            search_requested, effective_prompt = extract_search_query(effective_prompt)
            if not effective_prompt:
                effective_prompt = "Reply using available context."
            latency_debug_enabled = interaction.user.id in self._latency_debug_enabled_users
            show_think_enabled = interaction.user.id in self._thinking_enabled_users
            task = self._active_requests.start(
                request_key,
                self._generate_reply(
                    guild_id=interaction.guild.id if interaction.guild else None,
                    channel_id=channel_id,
                    user_id=interaction.user.id,
                    user_name=interaction.user.display_name,
                    prompt=effective_prompt,
                    context_lines=context_lines,
                    source_message_id=None,
                    collect_latency_debug=latency_debug_enabled,
                    show_think_enabled=show_think_enabled,
                    search_requested=search_requested,
                ),
            )
            try:
                reply, metrics = await task
            except asyncio.CancelledError:
                await interaction.followup.send("Cancelled your active request.", ephemeral=True)
                return
            finally:
                self._active_requests.clear(request_key, task)
            if latency_debug_enabled and metrics is not None:
                metrics["context_fetch_ms"] = context_fetch_ms
                metrics["end_to_end_ms"] = self._elapsed_ms(request_started_at)
                reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
            reply = self._render_discord_emojis(reply, interaction.guild)
            await self._send_interaction_reply_chunks(interaction, reply)

        @self.tree.command(name="ping", description="Check whether the bot is online.", guild=guild)
        async def ping(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(format_ping_message(self.latency), ephemeral=True)

        @self.tree.command(name="help", description="Show commands and usage tips.", guild=guild)
        async def help_command(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(format_help_message(), ephemeral=True)

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
        memory_group = app_commands.Group(name="memory", description="Manage your memory settings")
        show_group = app_commands.Group(name="show", description="Toggle reply overlays")
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

        @show_group.command(name="debug", description="Toggle latency debug output for your replies.")
        @app_commands.describe(enabled="true to include timing diagnostics, false to disable them")
        async def show_debug(interaction: discord.Interaction, enabled: bool) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if enabled:
                self._latency_debug_enabled_users.add(interaction.user.id)
                await interaction.response.send_message(
                    "Latency debug enabled for your replies (resets on bot restart).",
                    ephemeral=True,
                )
                return
            self._latency_debug_enabled_users.discard(interaction.user.id)
            await interaction.response.send_message("Latency debug disabled.", ephemeral=True)

        @show_group.command(name="thinking", description="Toggle reasoning summary visibility for your replies.")
        @app_commands.describe(enabled="true to allow reasoning summary, false to hide it")
        async def show_thinking(interaction: discord.Interaction, enabled: bool) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if enabled:
                self._thinking_enabled_users.add(interaction.user.id)
                await interaction.response.send_message(
                    "Reasoning summary enabled for your replies (resets on bot restart).",
                    ephemeral=True,
                )
                return
            self._thinking_enabled_users.discard(interaction.user.id)
            await interaction.response.send_message("Reasoning summary disabled.", ephemeral=True)

        self.tree.add_command(show_group, guild=guild)

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
            announcement = build_changelog_announcement(self.settings)
            if announcement is None:
                await interaction.response.send_message(
                    "No changelog message is configured or discoverable. Set `CHANGELOG_MESSAGE` / `CHANGELOG_VERSION`, or ensure `.git` is available.",
                    ephemeral=True,
                )
                return
            if interaction.guild is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            async with self.database.session() as session:
                channel_id = await self._get_changelog_channel_id(session, guild_id=interaction.guild.id)
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

        @memory_group.command(name="on", description="Enable memory retrieval and storage for you.")
        async def memory_on(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                await self.memory_service.set_enabled(session, interaction.user.id, True)
                await session.commit()
            await interaction.response.send_message("Memory enabled for your future chats.", ephemeral=True)

        @memory_group.command(name="off", description="Disable memory retrieval and storage for you.")
        async def memory_off(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                await self.memory_service.set_enabled(session, interaction.user.id, False)
                await session.commit()
            await interaction.response.send_message(
                "Memory disabled. Existing memories are kept until you delete them.",
                ephemeral=True,
            )

        self.tree.add_command(memory_group, guild=guild)

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
                        current_datetime_text=current_datetime_text,
                        prompt=prompt,
                        context_block=context_block,
                        memories_block=self._format_memories(memories),
                        channel_alias_block=self._format_channel_aliases(channel_aliases),
                        search_requested=search_requested,
                    ),
                },
            ]
            text, reasoning_parts = await self._run_chat_with_tools(
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
        current_datetime_text: str,
        prompt: str,
        context_block: str,
        memories_block: str,
        channel_alias_block: str,
        search_requested: bool = False,
    ) -> str:
        prompt_text = (
            f"Current user: {user_name}\n\n"
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

    async def _run_chat_with_tools(
        self,
        *,
        session,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        search_requested: bool,
        metrics: dict[str, int | str] | None,
    ) -> tuple[str, list[str]]:
        tools = self._build_chat_tools()
        required_tools: set[str] = set()
        if search_requested:
            required_tools.add("web_search")
        used_tools: set[str] = set()
        latest_tool_results: list[str] = []
        reasoning_parts: list[str] = []
        if metrics is not None:
            metrics["tool_call_count"] = 0
        for _ in range(MAX_CHAT_TOOL_ITERATIONS + 1):
            chat_started_at = time.perf_counter()
            turn = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_chat_model,
                feature="chat_reply",
                max_tokens=self.settings.max_completion_tokens,
                temperature=0.7,
                messages=messages,
                tools=tools,
            )
            if metrics is not None:
                metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + self._elapsed_ms(chat_started_at)
                metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
                metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
                metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
                self._append_raw_tool_trace(metrics, turn.raw_text)
            reasoning_parts.extend(self._collect_reasoning(turn))
            usage_write_started_at = time.perf_counter()
            await record_usage(
                session,
                usage=turn.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            if metrics is not None:
                metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + self._elapsed_ms(
                    usage_write_started_at
                )
            if not turn.tool_calls:
                missing_required_tools = required_tools - used_tools
                if missing_required_tools:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Before answering, you still must call these tools at least once: "
                                + ", ".join(sorted(missing_required_tools))
                            ),
                        }
                    )
                    continue
                if turn.text:
                    return turn.text, reasoning_parts
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        }
                        for tool_call in turn.tool_calls
                    ],
                }
            )
            used_tools.update(tool_call.name for tool_call in turn.tool_calls)
            if metrics is not None:
                metrics["tool_call_count"] = int(metrics.get("tool_call_count", 0)) + len(turn.tool_calls)
            tool_results = await asyncio.gather(
                *[
                    self._execute_chat_tool_call(
                        session=session,
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        source_message_id=source_message_id,
                    )
                    for tool_call in turn.tool_calls
                ]
            )
            rendered_tool_results: list[str] = []
            for tool_call, (tool_result, tool_metrics) in zip(turn.tool_calls, tool_results):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )
                rendered_tool_results.append(f"{tool_call.name}:\n{tool_result}")
                latest_tool_results.append(tool_result)
                if metrics is not None:
                    for key, value in tool_metrics.items():
                        metrics[key] = int(metrics.get(key, 0)) + value
            if rendered_tool_results:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool results for continuation:\n"
                            + "\n\n".join(rendered_tool_results)
                            + "\n\nUse these results. Only call another tool if you still need one."
                        ),
                    }
                )

        text, final_reasoning = await self._force_final_answer(
            session=session,
            messages=messages,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            metrics=metrics,
            latest_tool_results=latest_tool_results,
        )
        reasoning_parts.extend(final_reasoning)
        return text, reasoning_parts

    async def _force_final_answer(
        self,
        *,
        session,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        latest_tool_results: list[str],
    ) -> tuple[str, list[str]]:
        final_messages = list(messages)
        final_messages.append(
            {
                "role": "user",
                "content": (
                    "Stop using tools now. Give the final answer directly from the tool results and context you already have."
                ),
            }
        )
        chat_started_at = time.perf_counter()
        turn = await self.llm_client.complete_chat_turn(
            model=self.settings.openai_chat_model,
            feature="chat_reply_final",
            max_tokens=self.settings.max_completion_tokens,
            temperature=0.4,
            messages=final_messages,
            tools=None,
        )
        if metrics is not None:
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + self._elapsed_ms(chat_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
            self._append_raw_tool_trace(metrics, turn.raw_text)
        reasoning_parts = self._collect_reasoning(turn)
        usage_write_started_at = time.perf_counter()
        await record_usage(
            session,
            usage=turn.usage,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        if metrics is not None:
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + self._elapsed_ms(
                usage_write_started_at
            )
        if turn.text:
            return turn.text, reasoning_parts
        if latest_tool_results:
            return latest_tool_results[-1], reasoning_parts
        return "I hit the tool-call limit for this reply. Try asking in a more focused way.", reasoning_parts

    def _collect_reasoning(self, turn) -> list[str]:
        parts: list[str] = []
        if turn.reasoning_content:
            parts.append(turn.reasoning_content)
        inline_think = extract_think_content(turn.raw_text)
        parts.extend(inline_think)
        return parts

    def _append_raw_tool_trace(self, metrics: dict[str, int | str], raw_text: str) -> None:
        cleaned = raw_text.strip()
        if not cleaned or "<|tool_call" not in cleaned:
            return
        existing = str(metrics.get("raw_tool_trace", "")).strip()
        if existing:
            metrics["raw_tool_trace"] = existing + "\n\n---\n\n" + cleaned
            return
        metrics["raw_tool_trace"] = cleaned

    def _build_chat_tools(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "Search the web for fresh public information and source snippets. "
                        "Prefer one comprehensive query first. Only issue another search if earlier results are insufficient or conflicting."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The focused web search query to run.",
                            }
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_reminder",
                    "description": (
                        "Create a future reminder for the current user in this channel. "
                        "Use this when the user asks to be reminded on a specific date or time. "
                        "Prefer ISO 8601 date-times with timezone offsets. Date-only values are allowed and default to 09:00 local time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "The short reminder text to send later.",
                            },
                            "remind_at": {
                                "type": "string",
                                "description": (
                                    "When to send the reminder. Use an ISO 8601 local date or date-time, "
                                    "for example 2026-03-22 or 2026-03-22T15:30:00-07:00."
                                ),
                            },
                        },
                        "required": ["message", "remind_at"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_channel_message",
                    "description": (
                        "Send a message into another channel in the current Discord server. "
                        "Use a configured channel alias or a numeric channel ID. "
                        "Only use this when the user explicitly wants you to post somewhere else."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "Known channel alias or numeric channel ID.",
                            },
                            "message": {
                                "type": "string",
                                "description": "The message to send into that channel.",
                            },
                        },
                        "required": ["channel", "message"],
                    },
                },
            },
        ]

    async def _execute_chat_tool_call(
        self,
        *,
        session,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, dict[str, int]]:
        query = self._parse_tool_query_argument(arguments)
        if tool_name == "web_search":
            if not query:
                return "Tool call failed because the query argument was missing or invalid.", {}
            started_at = time.perf_counter()
            result = await self._execute_web_search_tool(query=query)
            return result, {
                "web_search_ms": self._elapsed_ms(started_at),
                "web_search_query_count": 1,
            }
        if tool_name == "create_reminder":
            payload = self._parse_create_reminder_arguments(arguments)
            if payload is None:
                return "Reminder creation failed because `message` or `remind_at` was missing or invalid.", {}
            reminder_text, remind_at_text = payload
            started_at = time.perf_counter()
            result = await self._execute_create_reminder_tool(
                session=session,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                reminder_text=reminder_text,
                remind_at_text=remind_at_text,
            )
            return result, {
                "reminder_create_ms": self._elapsed_ms(started_at),
                "reminder_create_count": 1,
            }
        if tool_name == "send_channel_message":
            payload = self._parse_send_channel_message_arguments(arguments)
            if payload is None:
                return "Channel send failed because `channel` or `message` was missing or invalid.", {}
            channel_target, message_text = payload
            started_at = time.perf_counter()
            result = await self._execute_send_channel_message_tool(
                session=session,
                guild_id=guild_id,
                channel_target=channel_target,
                message_text=message_text,
            )
            return result, {
                "channel_send_ms": self._elapsed_ms(started_at),
                "channel_send_count": 1,
            }
        return f"Unknown tool `{tool_name}`.", {}

    async def _execute_web_search_tool(
        self,
        *,
        query: str,
    ) -> str:
        try:
            search_response = await self.tavily_client.search(query=query, max_results=5)
        except TavilyAPIKeyMissingError:
            return "Web search failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            return f"Web search for `{query}` failed because the Tavily request failed."
        except TavilyDataError:
            return f"Web search for `{query}` failed because the Tavily response was malformed."
        return format_tavily_search_message(search_response, max_items=3)

    def _parse_tool_query_argument(self, arguments: str) -> str | None:
        payload = parse_json_object_payload(arguments)
        if payload is None:
            return None
        query = str(payload.get("query", "")).strip()
        return query or None

    def _parse_create_reminder_arguments(self, arguments: str) -> tuple[str, str] | None:
        payload = parse_json_object_payload(arguments)
        if payload is None:
            return None
        reminder_text = str(payload.get("message", "")).strip()
        remind_at_text = str(payload.get("remind_at", "")).strip()
        if not reminder_text or not remind_at_text:
            return None
        return reminder_text, remind_at_text

    def _parse_send_channel_message_arguments(self, arguments: str) -> tuple[str, str] | None:
        payload = parse_json_object_payload(arguments)
        if payload is None:
            return None
        channel_target = str(payload.get("channel", "")).strip()
        message_text = str(payload.get("message", "")).strip()
        if not channel_target or not message_text:
            return None
        return channel_target, message_text

    async def _execute_create_reminder_tool(
        self,
        *,
        session,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        reminder_text: str,
        remind_at_text: str,
    ) -> str:
        if channel_id is None:
            return "Reminder creation failed because this channel could not be resolved."
        timezone_name = await self.memory_service.get_timezone_name(session, user_id)
        user_timezone = get_timezone(timezone_name)
        parsed = self.reminder_service.parse_remind_at(
            remind_at_text,
            now=datetime.now(timezone.utc).astimezone(user_timezone),
        )
        if parsed is None:
            return (
                "Reminder creation failed because `remind_at` was invalid. "
                "Use an ISO 8601 local date or date-time, like `2026-03-22` or `2026-03-22T15:30:00-07:00`."
            )
        remind_at_utc = parsed.remind_at.astimezone(timezone.utc)
        if remind_at_utc <= datetime.now(timezone.utc):
            return "Reminder creation failed because the requested time is not in the future."
        reminder = await self.reminder_service.create_reminder(
            session,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            reminder_text=reminder_text,
            remind_at=remind_at_utc,
        )
        local_remind_at = parsed.remind_at.astimezone(user_timezone)
        reminder_line = (
            f"Reminder `{reminder.id}` created for {local_remind_at.strftime('%Y-%m-%d %H:%M:%S %Z')}: "
            f"{reminder.reminder_text}"
        )
        if parsed.assumed_time:
            reminder_line += " (assumed 09:00 local time because only a date was provided)"
        if source_message_id is not None:
            jump_link = format_discord_message_link(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=source_message_id,
            )
            reminder_line += f"\nOriginal message: {jump_link}"
        return reminder_line

    async def _execute_send_channel_message_tool(
        self,
        *,
        session,
        guild_id: int | None,
        channel_target: str,
        message_text: str,
    ) -> str:
        if guild_id is None:
            return "Channel send failed because this request was not tied to a server."
        resolved_channel_id = await self.channel_alias_service.resolve_channel_id(
            session,
            guild_id=guild_id,
            channel=channel_target,
        )
        if resolved_channel_id is None:
            return "Channel send failed because that alias or channel ID is unknown in this server."
        channel = self.get_channel(resolved_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(resolved_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return f"Channel send failed because channel `{channel_target}` could not be fetched."
        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or channel_guild.id != guild_id:
            return "Channel send failed because the target channel is not in this server."
        try:
            await channel.send(message_text)
        except (discord.Forbidden, discord.HTTPException):
            return f"Channel send failed because the bot could not send to `{channel_target}`."
        return f"Sent message to <#{resolved_channel_id}>."

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round(max(time.perf_counter() - started_at, 0.0) * 1000)
