from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from nycti.config import Settings
from nycti.db.session import Database
from nycti.formatting import (
    append_debug_block,
    extract_search_query,
    extract_think_content,
    format_current_datetime_context,
    format_latency_debug_block,
    format_ping_message,
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
from nycti.tavily.client import TavilyClient
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
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
        self._active_requests = ActiveRequestRegistry()
        self._latency_debug_enabled_users: set[int] = set()
        self._thinking_enabled_users: set[int] = set()
        self._register_commands()

    async def setup_hook(self) -> None:
        await self.database.init_models()
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

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

        benchmark_group = app_commands.Group(name="benchmark", description="Run benchmark tasks")

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

        @self.tree.command(name="debug", description="Toggle latency debug output for your replies.", guild=guild)
        @app_commands.describe(enabled="true to include timing diagnostics, false to disable them")
        async def debug(interaction: discord.Interaction, enabled: bool) -> None:
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

        @self.tree.command(
            name="thinking",
            description="Toggle reasoning summary visibility for your replies.",
            guild=guild,
        )
        @app_commands.describe(enabled="true to allow reasoning summary, false to hide it")
        async def thinking(interaction: discord.Interaction, enabled: bool) -> None:
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

        @self.tree.command(name="memory_on", description="Enable memory retrieval and storage for you.", guild=guild)
        async def memory_on(interaction: discord.Interaction) -> None:
            if interaction.user is None:
                return
            async with self.database.session() as session:
                await self.memory_service.set_enabled(session, interaction.user.id, True)
                await session.commit()
            await interaction.response.send_message("Memory enabled for your future chats.", ephemeral=True)

        @self.tree.command(name="memory_off", description="Disable memory retrieval and storage for you.", guild=guild)
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
        current_datetime_text = format_current_datetime_context(datetime.now())
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        async with self.database.session() as session:
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
        search_requested: bool = False,
    ) -> str:
        prompt_text = (
            f"Current user: {user_name}\n\n"
            f"Current local date/time:\n{current_datetime_text}\n\n"
            f"Current request:\n{prompt}\n\n"
            f"Recent channel context:\n{context_block}\n\n"
            f"Relevant long-term memories:\n{memories_block}\n\n"
        )
        prompt_text += (
            "Available tools:\n"
            "- `web_search(query)`: use for fresh public web information when it would improve the answer. Prefer one comprehensive search first. Only search again if the first results are clearly insufficient or conflicting.\n"
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
    ) -> tuple[str, dict[str, int]]:
        query = self._parse_tool_query_argument(arguments)
        if not query:
            return "Tool call failed because the query argument was missing or invalid.", {}
        if tool_name == "web_search":
            started_at = time.perf_counter()
            result = await self._execute_web_search_tool(query=query)
            return result, {
                "web_search_ms": self._elapsed_ms(started_at),
                "web_search_query_count": 1,
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

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round(max(time.perf_counter() - started_at, 0.0) * 1000)
