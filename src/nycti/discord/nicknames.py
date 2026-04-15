from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.member_aliases import format_member_alias_list


def register_nickname_commands(bot: Any, *, guild: Any = None) -> None:
    nickname_group = app_commands.Group(name="nickname", description="Manage member nicknames and aliases")

    @nickname_group.command(name="add", description="Add or update a nickname for a server member.")
    @app_commands.describe(
        user="Discord member this alias refers to",
        alias="Nickname or shorthand, like GTS",
        note="Optional short context blurb",
    )
    async def nickname_add(
        interaction: discord.Interaction,
        user: discord.Member,
        alias: str,
        note: str = "",
    ) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage member aliases.",
                ephemeral=True,
            )
            return
        try:
            async with bot.database.session() as session:
                member_alias = await bot.member_alias_service.set_alias(
                    session,
                    guild_id=interaction.guild.id,
                    user_id=user.id,
                    alias=alias,
                    note=note,
                    created_by_id=interaction.user.id,
                )
                await session.commit()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Alias `{member_alias.alias}` now points to {user.mention}.",
            ephemeral=True,
        )

    @nickname_group.command(name="delete", description="Delete a member nickname by alias or ID.")
    @app_commands.describe(alias="Alias text or ID from `/nickname list`")
    async def nickname_delete(interaction: discord.Interaction, alias: str) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage member aliases.",
                ephemeral=True,
            )
            return
        async with bot.database.session() as session:
            deleted = await bot.member_alias_service.delete_alias(
                session,
                guild_id=interaction.guild.id,
                alias=alias,
            )
            await session.commit()
        message = "Member alias deleted." if deleted else "No member alias found for that alias or ID."
        await interaction.response.send_message(message, ephemeral=True)

    @nickname_group.command(name="list", description="List configured member nicknames.")
    async def nickname_list(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        async with bot.database.session() as session:
            aliases = await bot.member_alias_service.list_aliases(
                session,
                guild_id=interaction.guild.id,
            )
        await interaction.response.send_message(format_member_alias_list(aliases), ephemeral=True)

    bot.tree.add_command(nickname_group, guild=guild)
