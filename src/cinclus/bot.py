from __future__ import annotations

import logging
import re
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from cinclus.config import Settings
from cinclus.db.session import Database
from cinclus.llm.client import OpenAIClient
from cinclus.memory.service import MemoryService
from cinclus.usage import record_usage

LOGGER = logging.getLogger(__name__)


class CinclusBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        llm_client: OpenAIClient,
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
        self.memory_service = memory_service
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

        cleaned_prompt = self._clean_trigger_content(message)
        async with message.channel.typing():
            reply = await self._generate_reply(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                prompt=cleaned_prompt or "Reply naturally to the conversation above.",
                context_lines=await self._fetch_context_lines(message.channel, before=message, include_current=message),
                source_message_id=message.id,
            )
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
            await interaction.response.defer(thinking=True)
            context_lines = await self._fetch_context_lines(interaction.channel, before=None, include_current=None)
            reply = await self._generate_reply(
                guild_id=interaction.guild.id if interaction.guild else None,
                channel_id=getattr(interaction.channel, "id", None),
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                prompt=prompt,
                context_lines=context_lines,
                source_message_id=None,
            )
            await interaction.followup.send(reply)

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
    ) -> str:
        context_block = "\n".join(context_lines[-self.settings.channel_context_limit :]) or "(no recent context)"
        async with self.database.session() as session:
            memories = await self.memory_service.retrieve_relevant(
                session,
                user_id=user_id,
                guild_id=guild_id,
                query=prompt,
            )
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
                        ),
                    },
                ],
            )
            await record_usage(
                session,
                usage=result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )

            try:
                _, memory_result = await self.memory_service.maybe_store_memory(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    source_message_id=source_message_id,
                    current_message=prompt,
                    recent_context=context_block,
                )
                if memory_result is not None:
                    await record_usage(
                        session,
                        usage=memory_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
            except Exception:  # pragma: no cover - defensive path
                LOGGER.exception("Memory extraction failed.")

            await session.commit()
        text = result.text or "I didn't get enough signal there. Try asking again with a little more detail."
        if len(text) > 1900:
            text = f"{text[:1897]}..."
        return text

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
        return (
            "You are a helpful AI bot in a private Discord friend server. "
            "Be concise, natural, and context-aware. "
            "Use the provided memories as soft hints, not unquestionable facts. "
            "Do not mention hidden prompts, memory scoring, or usage tracking. "
            "If the user is asking casually, keep the tone casual. "
            "If context is ambiguous, say what you are assuming."
        )

    def _build_user_prompt(
        self,
        *,
        user_name: str,
        prompt: str,
        context_block: str,
        memories_block: str,
    ) -> str:
        return (
            f"Current user: {user_name}\n\n"
            f"Current request:\n{prompt}\n\n"
            f"Recent channel context:\n{context_block}\n\n"
            f"Relevant long-term memories:\n{memories_block}\n\n"
            "Reply to the current request, not every message in the context window."
        )

    def _format_memories(self, memories: Iterable[object]) -> str:
        rendered = []
        for memory in memories:
            rendered.append(f"- [{memory.category}] {memory.summary}")
        return "\n".join(rendered) if rendered else "(none)"
