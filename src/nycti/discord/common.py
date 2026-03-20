from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord


SERVER_ONLY_MESSAGE = "This command only works in a server channel."


def can_manage_guild(user: "discord.abc.User | discord.Member | None") -> bool:
    import discord

    if user is None:
        return False
    if isinstance(user, discord.Member):
        return user.guild_permissions.manage_guild
    return True
