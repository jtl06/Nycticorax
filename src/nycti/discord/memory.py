from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.permissions import can_manage_user_memories, can_view_user_memories


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
        userid="Optional user ID for admin memory/profile actions",
        profile="true to view the compact personal profile note",
        clear_profile="true to clear the compact personal profile note",
    )
    async def memory(
        interaction: discord.Interaction,
        enable: bool | None = None,
        forget: int | None = None,
        userid: str | None = None,
        profile: bool | None = None,
        clear_profile: bool | None = None,
    ) -> None:
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
        if enable is None and forget is None and profile is None and clear_profile is None:
            await interaction.response.send_message(
                "Use `/memory enable:<true|false>`, `/memory forget:<id>`, `/memory profile:<true>`, or `/memory clear_profile:<true>`.",
                ephemeral=True,
            )
            return
        selected_actions = sum(
            action is not None
            for action in (enable, forget, profile, clear_profile)
        )
        if selected_actions > 1:
            await interaction.response.send_message(
                "Use only one memory action at a time.",
                ephemeral=True,
            )
            return
        if target_user_id != interaction.user.id and not can_manage_user_memories(
            requester_id=interaction.user.id,
            target_user_id=target_user_id,
            admin_user_id=bot.settings.discord_admin_user_id,
        ):
            await interaction.response.send_message(
                "You can only manage your own memory unless your user ID is configured as `DISCORD_ADMIN_USER_ID`.",
                ephemeral=True,
            )
            return
        if forget is not None:
            async with bot.database.session() as session:
                deleted = await bot.memory_service.delete_memory(session, target_user_id, forget)
                await session.commit()
            message = "Memory deleted." if deleted else "No memory found for that ID."
            await interaction.response.send_message(message, ephemeral=True)
            return
        if profile:
            if not can_view_user_memories(
                requester_id=interaction.user.id,
                target_user_id=target_user_id,
                admin_user_id=bot.settings.discord_admin_user_id,
            ):
                await interaction.response.send_message(
                    "You can only view your own profile unless your user ID is configured as `DISCORD_ADMIN_USER_ID`.",
                    ephemeral=True,
                )
                return
            async with bot.database.session() as session:
                profile_md = await bot.memory_service.get_personal_profile_md(session, target_user_id)
            rendered = profile_md or "No compact profile note stored yet."
            await interaction.response.send_message(rendered, ephemeral=True)
            return
        if clear_profile:
            async with bot.database.session() as session:
                cleared = await bot.memory_service.clear_personal_profile(session, target_user_id)
                await session.commit()
            message = "Compact profile cleared." if cleared else "No compact profile note was stored."
            await interaction.response.send_message(message, ephemeral=True)
            return
        if target_user_id != interaction.user.id:
            await interaction.response.send_message(
                "`enable` can only be changed for yourself.",
                ephemeral=True,
            )
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
