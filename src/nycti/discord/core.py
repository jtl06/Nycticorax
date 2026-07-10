from __future__ import annotations

from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.chat.run_state import AnswerProfile
from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.discord.live_benchmarks import register_live_benchmark_commands
from nycti.formatting import format_ping_message
from nycti.prompts import get_system_prompt

DEPTH_CHOICES = (
    ("Automatic", "auto"),
    ("Quick", "quick"),
    ("Grounded", "grounded"),
    ("Deep", "deep"),
)


def set_user_depth_preference(bot: Any, *, user_id: int, mode: str) -> AnswerProfile | None:
    normalized = mode.strip().casefold()
    if normalized == "auto":
        bot._depth_preferences.pop(user_id, None)
        return None
    profile = AnswerProfile(normalized)
    bot._depth_preferences[user_id] = profile
    return profile


def format_runtime_preference_status(bot: Any, *, user_id: int) -> str:
    depth = bot._depth_preferences.get(user_id)
    return (
        "Runtime preferences — "
        f"answer depth: `{str(depth) if depth is not None else 'auto'}`; "
        f"latency debug: `{'on' if user_id in bot._latency_debug_enabled_users else 'off'}`; "
        f"memory debug: `{'on' if user_id in bot._memory_debug_enabled_users else 'off'}`; "
        f"reasoning summary: `{'on' if user_id in bot._thinking_enabled_users else 'off'}`. "
        "These preferences reset on bot restart."
    )


def register_core_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="ping", description="Check whether the bot is online.", guild=guild)
    async def ping(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(format_ping_message(bot.latency), ephemeral=True)

    @bot.tree.command(name="show", description="Toggle reply overlays for your replies.", guild=guild)
    @app_commands.describe(
        debug="true to include timing diagnostics, false to disable them",
        memory="true to include memory retrieval diagnostics, false to disable them",
        thinking="true to allow reasoning summary, false to hide it",
    )
    async def show(
        interaction: discord.Interaction,
        debug: bool | None = None,
        memory: bool | None = None,
        thinking: bool | None = None,
    ) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if debug is None and memory is None and thinking is None:
            await interaction.response.send_message(
                format_runtime_preference_status(bot, user_id=interaction.user.id),
                ephemeral=True,
            )
            return

        messages: list[str] = []
        if debug is not None:
            if debug:
                bot._latency_debug_enabled_users.add(interaction.user.id)
                messages.append("Latency debug enabled.")
            else:
                bot._latency_debug_enabled_users.discard(interaction.user.id)
                messages.append("Latency debug disabled.")
        if memory is not None:
            if memory:
                bot._memory_debug_enabled_users.add(interaction.user.id)
                messages.append("Memory debug enabled.")
            else:
                bot._memory_debug_enabled_users.discard(interaction.user.id)
                messages.append("Memory debug disabled.")
        if thinking is not None:
            if thinking:
                bot._thinking_enabled_users.add(interaction.user.id)
                messages.append("Reasoning summary enabled.")
            else:
                bot._thinking_enabled_users.discard(interaction.user.id)
                messages.append("Reasoning summary disabled.")
        messages.append("These toggles reset on bot restart.")
        await interaction.response.send_message(" ".join(messages), ephemeral=True)

    @bot.tree.command(name="depth", description="Set answer depth for your future requests.", guild=guild)
    @app_commands.describe(mode="Choose quick, grounded, deep, or automatic routing")
    @app_commands.choices(
        mode=[app_commands.Choice(name=name, value=value) for name, value in DEPTH_CHOICES]
    )
    async def depth(interaction: discord.Interaction, mode: str | None = None) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if mode is None:
            await interaction.response.send_message(
                format_runtime_preference_status(bot, user_id=interaction.user.id),
                ephemeral=True,
            )
            return
        try:
            profile = set_user_depth_preference(
                bot,
                user_id=interaction.user.id,
                mode=mode,
            )
        except ValueError:
            await interaction.response.send_message(
                "Choose `quick`, `grounded`, `deep`, or `auto`.",
                ephemeral=True,
            )
            return
        if profile is None:
            message = "Answer depth set to `auto`; Nycti will choose a profile for each request."
        else:
            message = (
                f"Answer depth set to `{profile}` for your future requests. "
                "Use `/depth mode:auto` to restore automatic routing."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="cancel", description="Cancel your active prompt in this channel.", guild=guild)
    async def cancel(interaction: discord.Interaction) -> None:
        if interaction.channel is None or interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        channel_id = getattr(interaction.channel, "id", None)
        if channel_id is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        cancelled = bot._active_requests.cancel((channel_id, interaction.user.id))
        await interaction.response.send_message(
            "Cancelling your active request."
            if cancelled
            else "You do not have an active request in this channel.",
            ephemeral=True,
        )

    @bot.tree.command(name="cancel_all", description="Cancel all active in-flight prompts.", guild=guild)
    @app_commands.guild_only()
    async def cancel_all(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to cancel all active prompts.",
                ephemeral=True,
            )
            return
        cancelled_count = bot._active_requests.cancel_all()
        if cancelled_count == 0:
            await interaction.response.send_message("No active prompts to cancel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Cancelling `{cancelled_count}` active prompt(s).",
            ephemeral=True,
        )

    @bot.tree.command(name="reset", description="Hard reset runtime state for the bot.", guild=guild)
    @app_commands.guild_only()
    async def reset(interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.guild is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to reset bot runtime state.",
                ephemeral=True,
            )
            return
        cancelled_count = bot._active_requests.cancel_all()
        bot._latency_debug_enabled_users.clear()
        bot._memory_debug_enabled_users.clear()
        bot._thinking_enabled_users.clear()
        bot._depth_preferences.clear()
        get_system_prompt.cache_clear()
        await interaction.response.send_message(
            (
                "Runtime reset complete. "
                f"Cancelled `{cancelled_count}` active prompt(s), cleared per-user preferences, "
                "and refreshed the cached system prompt for the next request."
            ),
            ephemeral=True,
        )

    benchmark_group = app_commands.Group(name="benchmark", description="Run benchmark tasks")

    register_live_benchmark_commands(bot, benchmark_group)
    bot.tree.add_command(benchmark_group, guild=guild)
