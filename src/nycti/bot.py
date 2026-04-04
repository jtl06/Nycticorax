from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from urllib import request as urllib_request

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
    IMAGE_ANALYSIS_UNAVAILABLE,
    NO_IMAGE_ANALYSIS,
    append_debug_block,
    build_multimodal_user_content,
    extract_image_attachment_urls,
    extract_search_query,
    format_discord_message_link,
    format_latency_debug_block,
    format_memory_debug_block,
    format_thinking_block,
    model_requires_data_uri_image_input,
    normalize_discord_tables,
    parse_discord_message_links,
    render_custom_emoji_aliases,
    should_include_images_in_chat_request,
    split_message_chunks,
    strip_think_blocks,
)
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.prompts import get_system_prompt
from nycti.request_control import ActiveRequestRegistry
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
ALLOWED_CUSTOM_EMOJI_ALIASES = ("pepebeat", "pepeww", "kekw", "javsigh")
MAX_REPLY_CHAIN_DEPTH = 3
MAX_LINKED_MESSAGE_COUNT = 3
MAX_CONTEXT_IMAGE_COUNT = 3
MAX_IMAGE_DATA_URI_BYTES = 5 * 1024 * 1024
IMAGE_FETCH_TIMEOUT_SECONDS = 15


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
            database=database,
            llm_client=llm_client,
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

        cleaned_prompt = self._clean_trigger_content(message)
        effective_prompt = cleaned_prompt or "Reply naturally to the conversation above."
        search_requested, effective_prompt = extract_search_query(effective_prompt)
        if not effective_prompt:
            effective_prompt = "Reply naturally to the conversation above."
        request_started_at = time.perf_counter()
        context_started_at = time.perf_counter()
        context_lines, context_image_urls, image_context_lines = await self._build_message_context(
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
            vision_context_block = await self._build_vision_context(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                prompt=prompt,
                image_attachment_urls=image_attachment_urls,
                image_context_lines=image_context_lines,
                metrics=metrics,
            )
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
            await self._prepare_image_inputs_for_model(
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

    async def _build_vision_context(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        prompt: str,
        image_attachment_urls: list[str],
        image_context_lines: list[str],
        metrics: dict[str, int | str] | None,
    ) -> str:
        if not image_attachment_urls or not self.settings.openai_vision_model:
            return NO_IMAGE_ANALYSIS
        vision_prompt = (
            "Describe the included Discord images for a text-only assistant. "
            "Do not answer the user's full question. Only summarize what is visibly in the images, "
            "and match observations to the provided image labels when possible.\n\n"
            f"User request:\n{prompt}\n\n"
            f"Included image context:\n{chr(10).join(image_context_lines) or '(none)'}"
        )
        started_at = time.perf_counter()
        vision_image_inputs = await self._prepare_image_inputs_for_model(
            model=self.settings.openai_vision_model,
            image_urls=image_attachment_urls,
        )
        if not vision_image_inputs:
            LOGGER.warning(
                "Vision context skipped for model %s because no image inputs remained after preprocessing.",
                self.settings.openai_vision_model,
            )
            return IMAGE_ANALYSIS_UNAVAILABLE
        try:
            result = await self.llm_client.complete_chat(
                model=self.settings.openai_vision_model,
                feature="vision_context",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a vision analysis assistant. "
                            "Summarize visible image details for another assistant. "
                            "Be concrete, concise, and explicit about uncertainty."
                        ),
                    },
                    {
                        "role": "user",
                        "content": build_multimodal_user_content(vision_prompt, vision_image_inputs),
                    },
                ],
                max_tokens=min(self.settings.max_completion_tokens, 500),
                temperature=0.2,
            )
        except Exception as exc:
            LOGGER.exception(
                "Vision context generation failed for model %s with %s image(s): %s. Continuing without image analysis.",
                self.settings.openai_vision_model,
                len(image_attachment_urls),
                exc,
            )
            return IMAGE_ANALYSIS_UNAVAILABLE
        async with self.database.session() as session:
            await record_usage(
                session,
                usage=result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            await session.commit()
        if metrics is not None:
            metrics["vision_summary_ms"] = self._elapsed_ms(started_at)
        return result.text.strip() or IMAGE_ANALYSIS_UNAVAILABLE

    async def _prepare_image_inputs_for_model(
        self,
        *,
        model: str | None,
        image_urls: list[str],
    ) -> list[str]:
        if not image_urls:
            return []
        if not model_requires_data_uri_image_input(model):
            return image_urls
        converted = await asyncio.gather(
            *(self._download_image_as_data_uri(url) for url in image_urls),
            return_exceptions=True,
        )
        prepared: list[str] = []
        for image_url, result in zip(image_urls, converted):
            if isinstance(result, Exception):
                LOGGER.warning(
                    "Failed to convert image URL to data URI for model %s: %s (%s)",
                    model,
                    image_url,
                    result,
                )
                continue
            if not result:
                LOGGER.warning(
                    "Failed to convert image URL to data URI for model %s: %s",
                    model,
                    image_url,
                )
                continue
            prepared.append(result)
        return prepared

    async def _download_image_as_data_uri(self, image_url: str) -> str | None:
        return await asyncio.to_thread(self._download_image_as_data_uri_sync, image_url)

    @staticmethod
    def _download_image_as_data_uri_sync(image_url: str) -> str | None:
        request = urllib_request.Request(
            image_url,
            headers={"User-Agent": "Nycti/1.0"},
            method="GET",
        )
        with urllib_request.urlopen(request, timeout=IMAGE_FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            media_type = content_type if content_type.startswith("image/") else None
            if media_type is None:
                guessed_type, _ = mimetypes.guess_type(image_url)
                if guessed_type and guessed_type.startswith("image/"):
                    media_type = guessed_type
            if media_type is None:
                raise ValueError("response was not an image")
            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_DATA_URI_BYTES:
                    raise ValueError(
                        f"image exceeded {MAX_IMAGE_DATA_URI_BYTES} byte data URI limit"
                    )
                chunks.append(chunk)
        encoded = base64.b64encode(b"".join(chunks)).decode("ascii")
        return f"data:{media_type};base64,{encoded}"

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

    async def _fetch_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        history: list[discord.Message] = []
        async for item in channel.history(limit=self.settings.channel_context_limit, before=before, oldest_first=False):
            history.append(item)
        history.reverse()
        return history

    async def _fetch_context_lines(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
        include_current: discord.Message | None,
    ) -> list[str]:
        history = await self._fetch_context_messages(channel, before=before)

        lines = [self._format_message_line(message) for message in history if self._message_has_visible_content(message)]
        if include_current is not None and self._message_has_visible_content(include_current):
            lines.append(self._format_message_line(include_current))
        return lines

    async def _build_message_context(self, message: discord.Message) -> tuple[list[str], list[str], list[str]]:
        history_messages = await self._fetch_context_messages(
            message.channel,
            before=message,
        )
        history_lines = [
            self._format_message_line(item)
            for item in history_messages
            if self._message_has_visible_content(item)
        ]
        if self._message_has_visible_content(message):
            history_lines.append(self._format_message_line(message))
        reply_chain_messages = await self._collect_reply_chain_messages(message, max_depth=MAX_REPLY_CHAIN_DEPTH)
        reply_lines = [
            self._format_message_line(
                item,
                prefix=f"reply depth {depth}",
            )
            for depth, item in enumerate(reply_chain_messages, start=1)
            if self._message_has_visible_content(item)
        ]
        linked_messages = await self._collect_linked_messages(
            message,
            reply_chain_messages=reply_chain_messages,
            max_count=MAX_LINKED_MESSAGE_COUNT,
        )
        linked_lines = [
            self._format_message_line(item, prefix="linked message")
            for item in linked_messages
            if self._message_has_visible_content(item)
        ]
        context_lines = self._dedupe_lines(reply_lines + linked_lines + history_lines)
        image_refs: list[tuple[str, str]] = []
        image_refs.extend(self._image_refs_for_message(message, label="current message"))
        for depth, item in enumerate(reply_chain_messages, start=1):
            image_refs.extend(self._image_refs_for_message(item, label=f"reply depth {depth}"))
        for item in linked_messages:
            image_refs.extend(self._image_refs_for_message(item, label="linked message"))
        for item in history_messages:
            image_refs.extend(self._image_refs_for_message(item, label="recent context"))
        deduped_image_refs = self._dedupe_image_refs(image_refs)
        image_urls = [url for _, url in deduped_image_refs]
        image_context_lines = [
            f"- image {index}: {label}"
            for index, (label, _) in enumerate(deduped_image_refs, start=1)
        ]
        return context_lines, image_urls, image_context_lines

    async def _collect_reply_chain_messages(
        self,
        message: discord.Message,
        *,
        max_depth: int,
    ) -> list[discord.Message]:
        chain: list[discord.Message] = []
        seen_ids: set[int] = set()
        current = message
        for _ in range(max_depth):
            referenced = await self._resolve_referenced_message(current)
            if referenced is None or referenced.id in seen_ids:
                break
            seen_ids.add(referenced.id)
            chain.append(referenced)
            current = referenced
        return chain

    async def _collect_linked_messages(
        self,
        message: discord.Message,
        *,
        reply_chain_messages: list[discord.Message],
        max_count: int,
    ) -> list[discord.Message]:
        linked_messages: list[discord.Message] = []
        seen_ids = {message.id, *(item.id for item in reply_chain_messages)}
        search_messages = [message, *reply_chain_messages]
        for source in search_messages:
            links = parse_discord_message_links(source.content, guild_id=message.guild.id if message.guild else None)
            for channel_id, message_id in links:
                resolved = await self._fetch_linked_message(
                    guild=message.guild,
                    fallback_channel=message.channel,
                    channel_id=channel_id,
                    message_id=message_id,
                )
                if resolved is None or resolved.id in seen_ids:
                    continue
                seen_ids.add(resolved.id)
                linked_messages.append(resolved)
                if len(linked_messages) >= max_count:
                    return linked_messages
        return linked_messages

    async def _resolve_referenced_message(self, message: discord.Message) -> discord.Message | None:
        if message.reference is None or message.reference.message_id is None:
            return None
        referenced = message.reference.resolved
        if isinstance(referenced, discord.Message):
            return referenced
        try:
            return await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _fetch_linked_message(
        self,
        *,
        guild: discord.Guild | None,
        fallback_channel: discord.abc.Messageable,
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        channel: discord.abc.Messageable | None
        if getattr(fallback_channel, "id", None) == channel_id:
            channel = fallback_channel
        else:
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return None
        if guild is not None and getattr(channel, "guild", None) not in (None, guild):
            return None
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            return await fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _message_has_visible_content(self, message: discord.Message) -> bool:
        return bool(message.content.strip() or message.attachments)

    def _format_message_line(self, message: discord.Message, *, prefix: str | None = None) -> str:
        content = " ".join(message.content.split())
        if not content and message.attachments:
            content = f"[{len(message.attachments)} attachment(s)]"
        if len(content) > 400:
            content = f"{content[:397]}..."
        label = f"[{prefix}] " if prefix else ""
        return f"{label}{message.author.display_name}: {content}"

    @staticmethod
    def _dedupe_lines(lines: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            deduped.append(line)
        return deduped

    @staticmethod
    def _dedupe_image_urls(urls: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
            if len(deduped) >= MAX_CONTEXT_IMAGE_COUNT:
                break
        return deduped

    def _image_refs_for_message(self, message: discord.Message, *, label: str) -> list[tuple[str, str]]:
        return [
            (f"{label} from {message.author.display_name}", url)
            for url in extract_image_attachment_urls(message.attachments, limit=MAX_CONTEXT_IMAGE_COUNT)
        ]

    @staticmethod
    def _dedupe_image_refs(image_refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        deduped: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for label, url in image_refs:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append((label, url))
            if len(deduped) >= MAX_CONTEXT_IMAGE_COUNT:
                break
        return deduped

    def _clean_trigger_content(self, message: discord.Message) -> str:
        content = message.content
        if self.user is not None:
            content = re.sub(rf"<@!?{self.user.id}>", "", content)
        return " ".join(content.split()).strip()

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
