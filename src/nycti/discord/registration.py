from __future__ import annotations

from typing import Any

from nycti.discord.channels import register_channel_commands
from nycti.discord.config import register_config_commands
from nycti.discord.core import register_core_commands
from nycti.discord.help import register_help_command
from nycti.discord.memory import register_memory_commands
from nycti.discord.nicknames import register_nickname_commands
from nycti.discord.reminders import register_reminder_commands
from nycti.discord.rss import register_rss_commands
from nycti.discord.testing import register_testing_commands


def register_bot_commands(bot: Any, *, guild: Any = None) -> None:
    register_help_command(bot.tree, guild=guild)
    register_core_commands(bot, guild=guild)
    register_reminder_commands(bot, guild=guild)
    register_config_commands(bot, guild=guild)
    register_testing_commands(bot, guild=guild)
    register_memory_commands(bot, guild=guild)
    register_channel_commands(bot, guild=guild)
    register_rss_commands(bot, guild=guild)
    register_nickname_commands(bot, guild=guild)
