from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import discord
    from discord import app_commands


def format_help_message(page: int = 1) -> str:
    pages = {
        1: (
            "**Nycti Help 1/3**\n"
            "Triggers:\n"
            "- mention the bot\n"
            "- reply to a bot message\n\n"
            "Core commands:\n"
            "- `/help page:<1-3>`: show a help page\n"
            "- `/ping`: verify the bot is online\n"
            "- `/show debug:<true|false> [thinking:<true|false>]`: toggle debug overlays\n"
            "- `/benchmark earnings`: run the built-in no-context earnings benchmark\n"
            "- `/cancel_all`: cancel all in-flight prompts (`Manage Server` required)\n"
            "- `/reset`: clear runtime state and active prompts (`Manage Server` required)\n\n"
            "Next:\n"
            "- `/help page:2` for memory + reminders\n"
            "- `/help page:3` for channels, changelog, and tips"
        ),
        2: (
            "**Nycti Help 2/3**\n"
            "Memory:\n"
            "- `/memories`: list your stored memories\n"
            "- `/forget memory_id:<id>`: delete one memory\n"
            "- `/memory enabled:<true|false>`: enable or disable memory retrieval/storage\n\n"
            "Reminders:\n"
            "- ask naturally in chat, for example:\n"
            "  `@Nycti remind me on 2026-03-25 to check NVDA earnings`\n"
            "- `/reminders`: list your pending reminders\n"
            "- `/reminders_all`: list all pending reminders in this server (`Manage Server` required)\n"
            "- `/forget_reminder reminder_id:<id>`: delete one of your pending reminders\n"
            "- `/config time timezone:<zone>`: set your timezone, for example `PST` or `America/Los_Angeles`\n\n"
            "Next:\n"
            "- `/help page:1` for core commands\n"
            "- `/help page:3` for channels, changelog, and tips"
        ),
        3: (
            "**Nycti Help 3/3**\n"
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
            "- debug/thinking toggles are per-user and reset on bot restart\n\n"
            "Next:\n"
            "- `/help page:1` for core commands\n"
            "- `/help page:2` for memory + reminders"
        ),
    }
    return pages.get(page, "Use `/help page:1`, `/help page:2`, or `/help page:3`.")


def register_help_command(tree: Any, *, guild: Any = None) -> None:
    import discord
    from discord import app_commands
    globals()["discord"] = discord

    @tree.command(name="help", description="Show commands and usage tips.", guild=guild)
    @app_commands.describe(page="Help page number: 1, 2, or 3")
    async def help_command(interaction: discord.Interaction, page: int = 1) -> None:
        if page not in (1, 2, 3):
            await interaction.response.send_message(
                "Use `/help page:1`, `/help page:2`, or `/help page:3`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(format_help_message(page), ephemeral=True)
