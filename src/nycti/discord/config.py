from __future__ import annotations

from typing import Any

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.timezones import canonicalize_timezone_name


def register_config_commands(bot: Any, *, guild: Any = None) -> None:
    import discord
    from discord import app_commands
    globals()["discord"] = discord

    config_group = app_commands.Group(name="config", description="Configure your bot settings")

    @config_group.command(name="time", description="Set your timezone for reminders and date context.")
    @app_commands.describe(timezone="Timezone like PST or America/Los_Angeles")
    async def config_time(interaction: discord.Interaction, timezone: str) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        canonical_timezone = canonicalize_timezone_name(timezone)
        if canonical_timezone is None:
            await interaction.response.send_message(
                "Unknown timezone. Use something like `PST`, `UTC`, or `America/Los_Angeles`.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            stored_timezone = await bot.memory_service.set_timezone_name(
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
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to configure the changelog channel.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            await bot._set_changelog_channel_id(
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

    bot.tree.add_command(config_group, guild=guild)
