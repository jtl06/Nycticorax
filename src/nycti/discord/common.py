from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord


SERVER_ONLY_MESSAGE = "This command only works in a server channel."
CONFIGURED_GUILD_ONLY_MESSAGE = "This bot is configured for a different server."


def is_configured_guild(*, guild_id: int | None, configured_guild_id: int | None) -> bool:
    return configured_guild_id is None or guild_id == configured_guild_id


def can_manage_guild(user: "discord.abc.User | discord.Member | None") -> bool:
    import discord

    if not isinstance(user, discord.Member):
        return False
    return bool(user.guild_permissions.manage_guild)
