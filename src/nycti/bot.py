from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select

from nycti.channel_aliases import ChannelAliasService
from nycti.changelog import build_changelog_announcement
from nycti.chat.context import ChatContextBuilder, build_user_prompt
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.config import Settings
from nycti.db.models import AppState
from nycti.db.session import Database
from nycti.discord import register_bot_commands
from nycti.formatting import (
    NO_IMAGE_ANALYSIS,
    append_debug_block,
    build_multimodal_user_content,
    extract_search_query,
    format_discord_message_link,
    format_latency_debug_block,
    format_memory_debug_block,
    format_thinking_block,
    normalize_discord_tables,
    render_custom_emoji_aliases,
    should_include_images_in_chat_request,
    split_message_chunks,
    strip_think_blocks,
)
from nycti.llm.client import OpenAIClient
from nycti.message_context import MessageContextCollector, clean_trigger_content
from nycti.memory.service import MemoryService
from nycti.prompts import get_system_prompt
from nycti.request_control import ActiveRequestRegistry
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.usage import record_usage
from nycti.vision import VisionContextService

LOGGER = logging.getLogger(__name__)
ALLOWED_CUSTOM_EMOJI_ALIASES = ("pepebeat", "pepeww", "kekw", "javsigh")
MAX_REPLY_CHAIN_DEPTH = 3
MAX_LINKED_MESSAGE_COUNT = 3
MAX_CONTEXT_IMAGE_COUNT = 3


class NyctiBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        llm_client: OpenAIClient,
        market_data_client: TwelveDataClient,
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
        self.market_data_client = market_data_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self._active_requests = ActiveRequestRegistry()
        self._chat_orchestrator = ChatOrchestrator(
            settings=settings,
            database=database,
            llm_client=llm_client,
            market_data_client=market_data_client,
            tavily_client=tavily_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            reminder_service=reminder_service,
            bot=self,
        )
        self._chat_context_builder = ChatContextBuilder(
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
        )
        self._message_context_collector = MessageContextCollector(
            bot=self,
            channel_context_limit=self.settings.channel_context_limit,
            max_reply_chain_depth=MAX_REPLY_CHAIN_DEPTH,
            max_linked_message_count=MAX_LINKED_MESSAGE_COUNT,
            max_context_image_count=MAX_CONTEXT_IMAGE_COUNT,
        )
        self._vision_context_service = VisionContextService(settings, llm_client)
        self._latency_debug_enabled_users: set[int] = set()
        self._memory_debug_enabled_users: set[int] = set()
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

        cleaned_prompt = clean_trigger_content(
            message,
            bot_user_id=self.user.id if self.user is not None else None,
        )
        effective_prompt = cleaned_prompt or "Reply naturally to the conversation above."
        search_requested, effective_prompt = extract_search_query(effective_prompt)
        if not effective_prompt:
            effective_prompt = "Reply naturally to the conversation above."
        request_started_at = time.perf_counter()
        context_started_at = time.perf_counter()
        context_lines, context_image_urls, image_context_lines = await self._message_context_collector.build_message_context(
            message,
        )
        context_fetch_ms = self._elapsed_ms(context_started_at)
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
                prompt=effective_prompt,
                context_lines=context_lines,
                image_attachment_urls=context_image_urls,
                image_context_lines=image_context_lines,
                source_message_id=message.id,
                collect_latency_debug=latency_debug_enabled,
                collect_memory_debug=memory_debug_enabled,
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
        collect_latency_debug: bool = False,
        collect_memory_debug: bool = False,
        show_think_enabled: bool = False,
        search_requested: bool = False,
        include_memories: bool = True,
    ) -> tuple[str, dict[str, int | str] | None]:
        reply_started_at = time.perf_counter()
        metrics: dict[str, int | str] | None = {} if collect_latency_debug else None
        selected_chat_model = self.settings.openai_chat_model
        vision_context_block = NO_IMAGE_ANALYSIS
        if image_attachment_urls and self.settings.openai_vision_model:
            vision_result = await self._vision_context_service.build_context(
                prompt=prompt,
                image_attachment_urls=image_attachment_urls,
                image_context_lines=image_context_lines,
            )
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
        if metrics is not None:
            metrics["chat_model"] = self.settings.openai_chat_model
            metrics["memory_model"] = self.settings.openai_memory_model
            metrics["vision_model"] = self.settings.openai_vision_model or "(none)"
            metrics["active_chat_model"] = selected_chat_model
            metrics["web_search_requested"] = "yes" if search_requested else "no"
            metrics["image_attachment_count"] = len(image_attachment_urls)
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        image_context_block = "\n".join(image_context_lines) or "(no included images)"
        async with self.database.session() as session:
            prepared_context = await self._chat_context_builder.prepare(
                session,
                guild_id=guild_id,
                user_id=user_id,
                prompt=prompt,
                include_memories=include_memories,
                now=datetime.now(timezone.utc),
            )
            commit_started_at = time.perf_counter()
            await session.commit()
            if metrics is not None:
                metrics["memory_retrieval_ms"] = prepared_context.memory_retrieval_ms
                metrics["chat_commit_ms"] = self._elapsed_ms(commit_started_at)
        user_prompt_text = build_user_prompt(
            user_name=user_name,
            user_id=user_id,
            user_global_name=user_global_name,
            current_datetime_text=prepared_context.current_datetime_text,
            prompt=prompt,
            context_block=context_block,
            image_context_block=image_context_block,
            vision_context_block=vision_context_block,
            memories_block=prepared_context.memories_block,
            channel_alias_block=prepared_context.channel_alias_block,
            search_requested=search_requested,
        )
        use_chat_model_image_input = should_include_images_in_chat_request(
            image_attachment_urls,
            vision_model=self.settings.openai_vision_model,
            vision_context_block=vision_context_block,
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
            search_requested=search_requested,
            metrics=metrics,
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
        text = normalize_discord_tables(text)
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

    def _build_system_prompt(self) -> str:
        return get_system_prompt()

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
