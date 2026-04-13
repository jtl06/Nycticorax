from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.rss.client import RSSFetchError, RSSParseError


def register_rss_commands(bot: Any, *, guild: Any = None) -> None:
    rss_group = app_commands.Group(name="rss", description="Manage RSS news feeds")

    @rss_group.command(name="add", description="Add an RSS/Atom feed to post into a channel.")
    @app_commands.describe(
        url="RSS or Atom feed URL",
        channel="Target channel; defaults to NEWS_CHANNEL_ID, then the current channel",
    )
    async def rss_add(
        interaction: discord.Interaction,
        url: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage RSS feeds.",
                ephemeral=True,
            )
            return
        if bot.rss_service is None:
            await interaction.response.send_message("RSS service is not available.", ephemeral=True)
            return
        channel_id = _resolve_target_channel_id(bot, interaction, channel)
        if channel_id is None:
            await interaction.response.send_message(
                "No target channel found. Pass `channel:` or configure `NEWS_CHANNEL_ID`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with bot.database.session() as session:
                feed = await bot.rss_service.add_feed(
                    session,
                    guild_id=interaction.guild.id,
                    channel_id=channel_id,
                    feed_url=url,
                    created_by_id=interaction.user.id,
                )
                await session.commit()
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (RSSFetchError, RSSParseError):
            await interaction.followup.send(
                "RSS feed could not be fetched or parsed. Check that the URL points directly to an RSS/Atom feed.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"RSS feed `{feed.id}` added for <#{feed.channel_id}>: {feed.feed_url}",
            ephemeral=True,
        )

    @rss_group.command(name="delete", description="Delete an RSS feed by ID.")
    @app_commands.describe(feed_id="Feed ID from `/rss list`")
    async def rss_delete(interaction: discord.Interaction, feed_id: int) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to manage RSS feeds.",
                ephemeral=True,
            )
            return
        if bot.rss_service is None:
            await interaction.response.send_message("RSS service is not available.", ephemeral=True)
            return
        async with bot.database.session() as session:
            deleted = await bot.rss_service.delete_feed(
                session,
                guild_id=interaction.guild.id,
                feed_id=feed_id,
            )
            await session.commit()
        message = "RSS feed deleted." if deleted else "No RSS feed found for that ID in this server."
        await interaction.response.send_message(message, ephemeral=True)

    @rss_group.command(name="list", description="List configured RSS feeds for this server.")
    async def rss_list(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if bot.rss_service is None:
            await interaction.response.send_message("RSS service is not available.", ephemeral=True)
            return
        async with bot.database.session() as session:
            feeds = await bot.rss_service.list_feeds(session, guild_id=interaction.guild.id)
        from nycti.rss.service import format_rss_feed_list

        await interaction.response.send_message(format_rss_feed_list(feeds), ephemeral=True)

    bot.tree.add_command(rss_group, guild=guild)


def _resolve_target_channel_id(
    bot: Any,
    interaction: discord.Interaction,
    channel: discord.TextChannel | None,
) -> int | None:
    if channel is not None:
        return channel.id
    if bot.settings.news_channel_id is not None:
        return bot.settings.news_channel_id
    current_channel_id = getattr(interaction.channel, "id", None)
    return int(current_channel_id) if current_channel_id is not None else None
