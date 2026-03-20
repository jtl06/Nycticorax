from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]


def register_memory_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="memories", description="Show your stored memories.", guild=guild)
    async def memories(interaction: discord.Interaction) -> None:
        if interaction.user is None:
            return
        async with bot.database.session() as session:
            memories_list = await bot.memory_service.list_memories(session, interaction.user.id, limit=10)
            await interaction.response.send_message(
                bot.memory_service.format_memory_list(memories_list),
                ephemeral=True,
            )

    @bot.tree.command(name="forget", description="Delete one of your stored memories by ID.", guild=guild)
    @app_commands.describe(memory_id="The memory ID shown by /memories.")
    async def forget(interaction: discord.Interaction, memory_id: int) -> None:
        if interaction.user is None:
            return
        async with bot.database.session() as session:
            deleted = await bot.memory_service.delete_memory(session, interaction.user.id, memory_id)
            await session.commit()
        message = "Memory deleted." if deleted else "No memory found for that ID."
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="memory", description="Enable or disable memory retrieval and storage for you.", guild=guild)
    @app_commands.describe(enabled="true to enable memory, false to disable it")
    async def memory(interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.user is None:
            return
        async with bot.database.session() as session:
            await bot.memory_service.set_enabled(session, interaction.user.id, enabled)
            await session.commit()
        if enabled:
            await interaction.response.send_message("Memory enabled for your future chats.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Memory disabled. Existing memories are kept until you delete them.",
            ephemeral=True,
        )
