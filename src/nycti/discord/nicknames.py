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
    @bot.tree.command(name="nickname", description="Manage member nicknames and aliases.", guild=guild)
    @app_commands.describe(
        action="add, delete, or list",
        user="Required for add: Discord member this alias refers to",
        alias="Required for add/delete: nickname shorthand or alias ID",
        note="Optional short context blurb for add",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="delete", value="delete"),
            app_commands.Choice(name="list", value="list"),
        ],
    )
    async def nickname_command(
        interaction: discord.Interaction,
        action: str,
        user: discord.Member | None = None,
        alias: str | None = None,
        note: str = "",
    ) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return

        if action == "list":
            async with bot.database.session() as session:
                aliases = await bot.member_alias_service.list_aliases(
                    session,
                    guild_id=interaction.guild.id,
                )
            await interaction.response.send_message(format_member_alias_list(aliases), ephemeral=True)
            return

        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage member aliases.",
                ephemeral=True,
            )
            return
        if action == "add":
            if user is None or not alias or not alias.strip():
                await interaction.response.send_message(
                    "For `action=add`, both `user` and `alias` are required.",
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
            return

        if action == "delete":
            if not alias or not alias.strip():
                await interaction.response.send_message(
                    "For `action=delete`, `alias` is required.",
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
            return

        await interaction.response.send_message(
            "Unknown action. Use add, delete, or list.",
            ephemeral=True,
        )
