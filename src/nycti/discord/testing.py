from __future__ import annotations

from typing import Any

from nycti.changelog import build_changelog_announcement
from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild


def register_testing_commands(bot: Any, *, guild: Any = None) -> None:
    from discord import app_commands

    test_group = app_commands.Group(name="test", description="Run test utilities")

    @test_group.command(name="changelog", description="Post the current changelog message to the changelog channel.")
    async def test_changelog(interaction) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to test changelog posting.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        async with bot.database.session() as session:
            channel_id = await bot._get_changelog_channel_id(session, guild_id=interaction.guild.id)
            previous_snapshot = await bot._get_last_changelog_snapshot(session, guild_id=interaction.guild.id)
        announcement = build_changelog_announcement(
            bot.settings,
            previous_snapshot=previous_snapshot,
        )
        if announcement is None:
            await interaction.response.send_message(
                "No new changelog lines are pending. Update `src/nycti/changelog.md`, or ensure `.git` is available for commit-message fallback.",
                ephemeral=True,
            )
            return
        if channel_id is None:
            await interaction.response.send_message(
                "No changelog channel is configured for this server. Use `/config changelog` first.",
                ephemeral=True,
            )
            return
        sent = await bot._post_changelog_announcement(channel_id, announcement.content)
        if not sent:
            await interaction.response.send_message("Failed to post the changelog test message.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Posted changelog test message to <#{channel_id}>.",
            ephemeral=True,
        )

    bot.tree.add_command(test_group, guild=guild)
