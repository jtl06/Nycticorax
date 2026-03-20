from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.formatting import format_reminder_list


def register_reminder_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="reminders", description="Show your pending reminders.", guild=guild)
    async def reminders(interaction: discord.Interaction) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        async with bot.database.session() as session:
            timezone_name = await bot.memory_service.get_timezone_name(session, interaction.user.id)
            reminders_list = await bot.reminder_service.list_pending_for_user(session, user_id=interaction.user.id)
        if not reminders_list:
            await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
            return
        await interaction.response.send_message(
            format_reminder_list(reminders_list, timezone_name=timezone_name),
            ephemeral=True,
        )

    @bot.tree.command(name="reminders_all", description="Show all pending reminders in this server.", guild=guild)
    async def reminders_all(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to view all pending reminders.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            timezone_name = await bot.memory_service.get_timezone_name(session, interaction.user.id)
            reminders_list = await bot.reminder_service.list_pending_for_guild(
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

    @bot.tree.command(name="forget_reminder", description="Delete one of your pending reminders by ID.", guild=guild)
    @app_commands.describe(reminder_id="The reminder ID shown by /reminders.")
    async def forget_reminder(interaction: discord.Interaction, reminder_id: int) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        async with bot.database.session() as session:
            deleted = await bot.reminder_service.delete_reminder(
                session,
                user_id=interaction.user.id,
                reminder_id=reminder_id,
            )
            await session.commit()
        message = "Reminder deleted." if deleted else "No pending reminder found for that ID."
        await interaction.response.send_message(message, ephemeral=True)
