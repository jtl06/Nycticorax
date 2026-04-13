from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]


def format_help_message(page: int = 1) -> str:
    pages = {
        1: (
            "**Nycti Help 1/2**\n"
            "Triggers:\n"
            "- mention the bot\n"
            "- say `nycti` in a message\n"
            "- reply to a bot message\n\n"
            "Core commands:\n"
            "- `/help page:<1-2>`: show a help page\n"
            "- `/ping`: verify the bot is online\n"
            "- `/show debug:<true|false> [memory:<true|false>] [thinking:<true|false>]`: toggle reply overlays\n"
            "- `/benchmark earnings`: run the built-in no-context earnings benchmark\n"
            "- `/cancel_all`: cancel all in-flight prompts (`Manage Server` required)\n"
            "- `/reset`: clear runtime state and active prompts (`Manage Server` required)\n"
            "- `/memories [userid:<id>]`: list your stored memories, or another user's if you're the configured admin\n"
            "- `/memory enable:<true|false>`: enable or disable memory\n"
            "- `/memory forget:<id> [userid:<id>]`: delete one memory by ID; `userid` is admin-only\n"
            "- `/memory profile:<true> [userid:<id>]`: view the compact profile note; `userid` is admin-only\n"
            "- `/memory clear_profile:<true> [userid:<id>]`: clear the compact profile note; `userid` is admin-only\n"
            "- ask naturally in chat for reminders\n"
            "- `/reminders`, `/reminders_all`, `/forget_reminder`\n"
            "- `/config time timezone:<zone>`: set your timezone\n\n"
            "Next:\n"
            "- `/help page:2` for channels, changelog, and tips"
        ),
        2: (
            "**Nycti Help 2/2**\n"
            "Channels / changelog:\n"
            "- `/config changelog channel:<channel>`: set or clear the startup changelog channel (`Manage Server` required)\n"
            "- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)\n"
            "- `/channel delete alias:<name>`: remove an alias (`Manage Server` required)\n"
            "- `/channel list`: show configured aliases\n"
            "- `/test changelog`: post the current changelog message to the configured changelog channel (`Manage Server` required)\n\n"
            "Tips:\n"
            "- include `use search` to force at least one web search\n"
            "- edit `src/nycti/changelog.md` before deploys if you want a custom startup changelog post\n"
            "- the bot only posts in other channels when you explicitly ask it to and it has permission there\n"
            "- reminders and date parsing use your configured timezone\n"
            "- debug/memory/thinking toggles are per-user and reset on bot restart\n\n"
            "Next:\n"
            "- `/help page:1` for core commands, memory, and reminders"
        ),
    }
    return pages.get(page, "Use `/help page:1` or `/help page:2`.")


def register_help_command(tree: Any, *, guild: Any = None) -> None:
    @tree.command(name="help", description="Show commands and usage tips.", guild=guild)
    @app_commands.describe(page="Help page number: 1 or 2")
    async def help_command(interaction: discord.Interaction, page: int = 1) -> None:
        if page not in (1, 2):
            await interaction.response.send_message(
                "Use `/help page:1` or `/help page:2`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(format_help_message(page), ephemeral=True)
