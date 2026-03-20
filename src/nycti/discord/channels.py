from __future__ import annotations

from typing import Any

from nycti.channel_aliases import normalize_channel_alias
from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.formatting import format_channel_alias_list


def register_channel_commands(bot: Any, *, guild: Any = None) -> None:
    import discord
    from discord import app_commands

    channel_group = app_commands.Group(name="channel", description="Manage cross-channel aliases")

    @channel_group.command(name="set", description="Set or update a channel alias.")
    @app_commands.describe(alias="Short alias like alerts", channel_id="Target Discord channel ID")
    async def channel_set(interaction: discord.Interaction, alias: str, channel_id: str) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage channel aliases.",
                ephemeral=True,
            )
            return
        normalized_alias = normalize_channel_alias(alias)
        if normalized_alias is None or not channel_id.isdigit():
            await interaction.response.send_message(
                "Alias must use letters, numbers, `-`, or `_`, and channel_id must be numeric.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            alias_row = await bot.channel_alias_service.set_alias(
                session,
                guild_id=interaction.guild.id,
                alias=normalized_alias,
                channel_id=int(channel_id),
            )
            await session.commit()
        await interaction.response.send_message(
            f"Alias `{alias_row.alias}` now points to <#{alias_row.channel_id}>.",
            ephemeral=True,
        )

    @channel_group.command(name="delete", description="Delete a channel alias.")
    @app_commands.describe(alias="Alias to remove")
    async def channel_delete(interaction: discord.Interaction, alias: str) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage channel aliases.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            deleted = await bot.channel_alias_service.delete_alias(
                session,
                guild_id=interaction.guild.id,
                alias=alias,
            )
            await session.commit()
        message = "Channel alias deleted." if deleted else "No channel alias found for that name."
        await interaction.response.send_message(message, ephemeral=True)

    @channel_group.command(name="list", description="List configured channel aliases.")
    async def channel_list(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        async with bot.database.session() as session:
            aliases = await bot.channel_alias_service.list_aliases(session, guild_id=interaction.guild.id)
        await interaction.response.send_message(format_channel_alias_list(aliases), ephemeral=True)

    bot.tree.add_command(channel_group, guild=guild)
