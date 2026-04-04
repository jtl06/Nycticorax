from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.permissions import can_view_user_memories


def register_memory_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="memories", description="Show stored memories.", guild=guild)
    @app_commands.describe(userid="Optional user ID to inspect. Admin only for other users.")
    async def memories(interaction: discord.Interaction, userid: str | None = None) -> None:
        if interaction.user is None:
            return
        target_user_id = interaction.user.id
        if userid is not None:
            normalized = userid.strip()
            if not normalized:
                await interaction.response.send_message("`userid` must be a Discord user ID.", ephemeral=True)
                return
            try:
                target_user_id = int(normalized)
            except ValueError:
                await interaction.response.send_message("`userid` must be a Discord user ID.", ephemeral=True)
                return
        if not can_view_user_memories(
            requester_id=interaction.user.id,
            target_user_id=target_user_id,
            admin_user_id=bot.settings.discord_admin_user_id,
        ):
            await interaction.response.send_message(
                "You can only view your own memories unless your user ID is configured as `DISCORD_ADMIN_USER_ID`.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            memories_list = await bot.memory_service.list_memories(session, target_user_id, limit=10)
            await interaction.response.send_message(
                bot.memory_service.format_memory_list(memories_list),
                ephemeral=True,
            )

    @bot.tree.command(name="memory", description="Enable/disable memory or forget one memory by ID.", guild=guild)
    @app_commands.describe(
        enable="true to enable memory, false to disable it",
        forget="The memory ID shown by /memories to delete",
    )
    async def memory(
        interaction: discord.Interaction,
        enable: bool | None = None,
        forget: int | None = None,
    ) -> None:
        if interaction.user is None:
            return
        if enable is None and forget is None:
            await interaction.response.send_message(
                "Use `/memory enable:<true|false>` or `/memory forget:<id>`.",
                ephemeral=True,
            )
            return
        if enable is not None and forget is not None:
            await interaction.response.send_message(
                "Use only one memory action at a time: either `enable` or `forget`.",
                ephemeral=True,
            )
            return
        if forget is not None:
            async with bot.database.session() as session:
                deleted = await bot.memory_service.delete_memory(session, interaction.user.id, forget)
                await session.commit()
            message = "Memory deleted." if deleted else "No memory found for that ID."
            await interaction.response.send_message(message, ephemeral=True)
            return
        async with bot.database.session() as session:
            await bot.memory_service.set_enabled(session, interaction.user.id, bool(enable))
            await session.commit()
        if enable:
            await interaction.response.send_message("Memory enabled for your future chats.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Memory disabled. Existing memories are kept until you delete them.",
            ephemeral=True,
        )
