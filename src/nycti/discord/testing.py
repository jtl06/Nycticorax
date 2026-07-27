from __future__ import annotations

from typing import Any

from nycti.changelog import build_changelog_announcement
from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.discord.rate_limits import (
    DISCORD_OUTBOUND_CIRCUIT_BREAKER,
    try_discord_request,
)


def register_testing_commands(bot: Any, *, guild: Any = None) -> None:
    from discord import app_commands

    test_group = app_commands.Group(name="test", description="Run test utilities")
    app_commands.guild_only(test_group)

    @test_group.command(name="changelog", description="Post the current changelog message to the changelog channel.")
    async def test_changelog(interaction) -> None:
        async def respond(content: str) -> bool:
            return await try_discord_request(
                lambda: interaction.response.send_message(content, ephemeral=True),
                circuit_breaker=DISCORD_OUTBOUND_CIRCUIT_BREAKER,
            )

        if interaction.user is None or interaction.guild is None:
            await respond(SERVER_ONLY_MESSAGE)
            return
        if not can_manage_guild(interaction.user):
            await respond("You need `Manage Server` permission to test changelog posting.")
            return
        async with bot.database.session() as session:
            channel_id = await bot._get_changelog_channel_id(session, guild_id=interaction.guild.id)
            previous_snapshot = await bot._get_last_changelog_snapshot(session, guild_id=interaction.guild.id)
        announcement = build_changelog_announcement(
            bot.settings,
            previous_snapshot=previous_snapshot,
        )
        if announcement is None:
            await respond(
                "No new changelog lines are pending. Update `src/nycti/changelog.md`, or ensure `.git` is available for commit-message fallback."
            )
            return
        if channel_id is None:
            await respond(
                "No changelog channel is configured for this server. Use `/config changelog` first."
            )
            return
        sent = await bot._post_changelog_announcement(channel_id, announcement.content)
        if not sent:
            await respond("Failed to post the changelog test message.")
            return
        await respond(f"Posted changelog test message to <#{channel_id}>.")

    bot.tree.add_command(test_group, guild=guild)
