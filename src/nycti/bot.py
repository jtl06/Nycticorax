from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from nycti.config import Settings
from nycti.db.session import Database
from nycti.formatting import (
    append_debug_block,
    extract_sec_query,
    extract_search_query,
    format_latency_debug_block,
    format_ping_message,
    render_custom_emoji_aliases,
    strip_think_blocks,
)
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.prompts import get_system_prompt
from nycti.request_control import ActiveRequestRegistry
from nycti.sec.client import SecClient
from nycti.sec.formatting import format_latest_filings_message
from nycti.sec.models import (
    SecDataError,
    SecHTTPError,
    SecNoFilingsError,
    SecTickerNotFoundError,
    SecUserAgentMissingError,
)
from nycti.tavily.client import TavilyClient
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
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
        sec_client: SecClient,
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
        self.sec_client = sec_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self._active_requests = ActiveRequestRegistry()
        self._latency_debug_enabled_users: set[int] = set()
        self._show_think_enabled_users: set[int] = set()
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
        sec_requested, effective_prompt = extract_sec_query(effective_prompt)
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
        show_think_enabled = message.author.id in self._show_think_enabled_users
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
                sec_requested=sec_requested,
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
            reply = append_debug_block(reply, format_latency_debug_block(metrics))
        reply = self._render_discord_emojis(reply, message.guild)
        await message.reply(reply, mention_author=False)

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
            sec_requested, effective_prompt = extract_sec_query(effective_prompt)
            if not effective_prompt:
                effective_prompt = "Reply using available context."
            latency_debug_enabled = interaction.user.id in self._latency_debug_enabled_users
            show_think_enabled = interaction.user.id in self._show_think_enabled_users
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
                    sec_requested=sec_requested,
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
                reply = append_debug_block(reply, format_latency_debug_block(metrics))
            reply = self._render_discord_emojis(reply, interaction.guild)
            await interaction.followup.send(reply)

        @self.tree.command(name="ping", description="Check whether the bot is online.", guild=guild)
        async def ping(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(format_ping_message(self.latency), ephemeral=True)

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
            name="show_think",
            description="Toggle reasoning summary visibility for your replies.",
            guild=guild,
        )
        @app_commands.describe(enabled="true to allow reasoning summary, false to hide it")
        async def show_think(interaction: discord.Interaction, enabled: bool) -> None:
            if interaction.user is None:
                await interaction.response.send_message("This command only works in a server channel.", ephemeral=True)
                return
            if enabled:
                self._show_think_enabled_users.add(interaction.user.id)
                await interaction.response.send_message(
                    "Reasoning summary enabled for your replies (resets on bot restart).",
                    ephemeral=True,
                )
                return
            self._show_think_enabled_users.discard(interaction.user.id)
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

        @self.tree.command(name="sec_latest", description="Look up the latest SEC filings for a ticker.", guild=guild)
        @app_commands.describe(ticker="Public company ticker symbol, like AAPL or MSFT.")
        async def sec_latest(interaction: discord.Interaction, ticker: str) -> None:
            if interaction.user is None or interaction.channel is None:
                await interaction.response.send_message(
                    "This command only works in a server channel.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                result = await self.sec_client.latest_filings(ticker=ticker, limit=5)
            except SecUserAgentMissingError:
                await interaction.followup.send(
                    "SEC_USER_AGENT is not configured. Set a contact-style user agent before using `/sec_latest`.",
                    ephemeral=True,
                )
                return
            except SecTickerNotFoundError:
                await interaction.followup.send(
                    f"Unknown ticker `{ticker.strip().upper()}`. I could not map it to an SEC registrant.",
                    ephemeral=True,
                )
                return
            except SecNoFilingsError:
                await interaction.followup.send(
                    f"No recent SEC filings were found for `{ticker.strip().upper()}`.",
                    ephemeral=True,
                )
                return
            except SecHTTPError:
                await interaction.followup.send(
                    "SEC request failed. Try again later.",
                    ephemeral=True,
                )
                return
            except SecDataError:
                await interaction.followup.send(
                    "SEC data was unavailable or malformed. Try again later.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                format_latest_filings_message(result),
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
        sec_requested: bool = False,
    ) -> tuple[str, dict[str, int | str] | None]:
        reply_started_at = time.perf_counter()
        metrics: dict[str, int | str] | None = {} if collect_latency_debug else None
        if metrics is not None:
            metrics["chat_model"] = self.settings.openai_chat_model
            metrics["memory_model"] = self.settings.openai_memory_model
            metrics["web_search_requested"] = "yes" if search_requested else "no"
            metrics["sec_requested"] = "yes" if sec_requested else "no"
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        web_results_block = "(not requested)"
        if search_requested:
            search_started_at = time.perf_counter()
            try:
                search_response = await self.tavily_client.search(query=prompt, max_results=5)
                web_results_block = format_tavily_search_message(search_response, max_items=3)
            except TavilyAPIKeyMissingError:
                web_results_block = "Web search requested but TAVILY_API_KEY is not configured."
            except TavilyHTTPError:
                web_results_block = "Web search requested but Tavily request failed."
            except TavilyDataError:
                web_results_block = "Web search requested but Tavily response was malformed."
            if metrics is not None:
                metrics["web_search_ms"] = self._elapsed_ms(search_started_at)
        sec_results_block = "(not requested)"
        if sec_requested:
            sec_started_at = time.perf_counter()
            try:
                sec_result = await self.sec_client.latest_filings_from_text(prompt, limit=5)
                sec_results_block = format_latest_filings_message(sec_result)
            except SecUserAgentMissingError:
                sec_results_block = "SEC lookup requested but SEC_USER_AGENT is not configured."
            except SecTickerNotFoundError:
                sec_results_block = "SEC lookup requested but no valid ticker was found in your prompt."
            except SecNoFilingsError:
                sec_results_block = "SEC lookup requested but no recent filings were found."
            except SecHTTPError:
                sec_results_block = "SEC lookup requested but SEC request failed."
            except SecDataError:
                sec_results_block = "SEC lookup requested but SEC response was malformed."
            if metrics is not None:
                metrics["sec_lookup_ms"] = self._elapsed_ms(sec_started_at)
        async with self.database.session() as session:
            retrieve_started_at = time.perf_counter()
            memories = await self.memory_service.retrieve_relevant(
                session,
                user_id=user_id,
                guild_id=guild_id,
                query=prompt,
            )
            if metrics is not None:
                metrics["memory_retrieval_ms"] = self._elapsed_ms(retrieve_started_at)
            chat_started_at = time.perf_counter()
            result = await self.llm_client.complete_chat(
                model=self.settings.openai_chat_model,
                feature="chat_reply",
                max_tokens=self.settings.max_completion_tokens,
                temperature=0.7,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {
                        "role": "user",
                        "content": self._build_user_prompt(
                            user_name=user_name,
                            prompt=prompt,
                            context_block=context_block,
                            memories_block=self._format_memories(memories),
                            show_think_enabled=show_think_enabled,
                            search_requested=search_requested,
                            web_results_block=web_results_block,
                            sec_requested=sec_requested,
                            sec_results_block=sec_results_block,
                        ),
                    },
                ],
            )
            if metrics is not None:
                metrics["chat_llm_ms"] = self._elapsed_ms(chat_started_at)
            usage_write_started_at = time.perf_counter()
            await record_usage(
                session,
                usage=result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            if metrics is not None:
                metrics["chat_usage_write_ms"] = self._elapsed_ms(usage_write_started_at)
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
        text = result.text or ""
        if not show_think_enabled:
            text = strip_think_blocks(text)
        if not text:
            text = "I didn't get enough signal there. Try asking again with a little more detail."
        if len(text) > 1900:
            text = f"{text[:1897]}..."
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
        prompt: str,
        context_block: str,
        memories_block: str,
        show_think_enabled: bool = False,
        search_requested: bool = False,
        web_results_block: str = "(not requested)",
        sec_requested: bool = False,
        sec_results_block: str = "(not requested)",
    ) -> str:
        prompt_text = (
            f"Current user: {user_name}\n\n"
            f"Current request:\n{prompt}\n\n"
            f"Recent channel context:\n{context_block}\n\n"
            f"Relevant long-term memories:\n{memories_block}\n\n"
        )
        if search_requested:
            prompt_text += f"Web search results (requested by user phrase 'use search'):\n{web_results_block}\n\n"
        if sec_requested:
            prompt_text += f"SEC filings lookup (requested by user phrase 'use sec'):\n{sec_results_block}\n\n"
        prompt_text += "Reply to the current request, not every message in the context window."
        if not show_think_enabled:
            return prompt_text
        return (
            prompt_text
            + "\n\n"
            + "Show-think mode is enabled. After your normal answer, include a short `debug_reasoning` section with:\n"
            + "- key_points: 1-3 concise bullets\n"
            + "- assumptions: brief list or `none`\n"
            + "Keep it compact and user-facing. Do not expose hidden/internal reasoning."
        )

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

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round(max(time.perf_counter() - started_at, 0.0) * 1000)
