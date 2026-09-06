from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from nycti.chat.action_confirmation import ActionKind, EmojiImportAction, render_action_proposal_card
from nycti.discord.common import can_manage_guild, is_configured_guild
from nycti.discord.emoji_import import can_create_emoji
from nycti.emoji_catalog import emoji_name

LOGGER = logging.getLogger(__name__)


async def manage_emoji(
    bot: Any, interaction: Any, *, action: str, emoji: str = "", alias: str = "", page: int = 1,
) -> str:
    guild = interaction.guild
    if guild is None or not is_configured_guild(
        guild_id=guild.id, configured_guild_id=bot.settings.discord_guild_id,
    ):
        return "Use this command in Nycti's configured server."
    catalog = bot.emoji_catalog
    if action == "list":
        return await catalog.listing(guild, page)
    if action in {"override", "delete"}:
        if not can_manage_guild(interaction.user):
            return "You need Manage Server permission to change emoji overrides."
        if action == "delete":
            await catalog.override(guild.id, alias, None)
            return "Emoji override removed. The server emoji itself was not deleted."
        resolved = await catalog.resolve(guild, emoji)
        await catalog.observe(guild.id, resolved.token)
        await catalog.override(guild.id, alias, resolved)
        available = catalog.replacements(guild).get(emoji_name(alias).casefold())
        return (f":{emoji_name(alias)}: now maps to {available}." if available else
                "Override saved. Import that external emoji before Nycti can render it.")
    if action == "import":
        if not can_create_emoji(interaction.user) or not can_create_emoji(guild.me):
            return "Emoji imports need Create Expressions or Manage Expressions for both you and Nycti."
        resolved = await catalog.resolve(guild, emoji)
        name = emoji_name(alias or resolved.name)
        await catalog.observe(guild.id, resolved.token)
        executor = bot._chat_orchestrator.tool_runner.executor
        proposal = await executor.action_confirmations.propose(
            kind=ActionKind.IMPORT_EMOJI,
            payload=EmojiImportAction(resolved.id, name, resolved.animated),
            guild_id=guild.id, request_channel_id=interaction.channel_id,
            user_id=interaction.user.id, source_message_id=None,
        )
        return render_action_proposal_card(proposal)
    return "Unknown action. Use list, override, delete, or import."


def register_emoji_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="emoji", description="View learned emojis, override aliases, or propose an import.", guild=guild)
    @app_commands.guild_only()
    @app_commands.choices(action=[app_commands.Choice(name=name, value=name)
                                  for name in ("list", "override", "delete", "import")])
    @app_commands.describe(
        emoji="Paste a custom emoji, learned name, or ID",
        alias="Alias to override/delete, or optional name for an import",
        page="Catalog page (12 entries per page)",
    )
    async def emoji_command(
        interaction: discord.Interaction, action: str, emoji: str = "", alias: str = "", page: int = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await manage_emoji(bot, interaction, action=action, emoji=emoji, alias=alias, page=page)
        except ValueError as exc:
            result = str(exc)
        except Exception:
            LOGGER.exception("Emoji catalog command failed.")
            result = "Couldn't update the emoji catalog. Please try again."
        await interaction.followup.send(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
