from __future__ import annotations

import asyncio
import time
from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.formatting import append_debug_block, format_latency_debug_block, format_ping_message
from nycti.prompts import get_system_prompt

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild


def register_core_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="ping", description="Check whether the bot is online.", guild=guild)
    async def ping(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(format_ping_message(bot.latency), ephemeral=True)

    @bot.tree.command(name="show", description="Toggle reply overlays for your replies.", guild=guild)
    @app_commands.describe(
        debug="true to include timing diagnostics, false to disable them",
        thinking="true to allow reasoning summary, false to hide it",
    )
    async def show(
        interaction: discord.Interaction,
        debug: bool | None = None,
        thinking: bool | None = None,
    ) -> None:
        if interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if debug is None and thinking is None:
            await interaction.response.send_message(
                "Provide `debug` and/or `thinking` as true or false.",
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
        if thinking is not None:
            if thinking:
                bot._thinking_enabled_users.add(interaction.user.id)
                messages.append("Reasoning summary enabled.")
            else:
                bot._thinking_enabled_users.discard(interaction.user.id)
                messages.append("Reasoning summary disabled.")
        messages.append("These toggles reset on bot restart.")
        await interaction.response.send_message(" ".join(messages), ephemeral=True)

    @bot.tree.command(name="cancel_all", description="Cancel all active in-flight prompts.", guild=guild)
    async def cancel_all(interaction: discord.Interaction) -> None:
        if interaction.user is None:
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
    async def reset(interaction: discord.Interaction) -> None:
        if interaction.user is None:
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
        bot._thinking_enabled_users.clear()
        get_system_prompt.cache_clear()
        await interaction.response.send_message(
            (
                "Runtime reset complete. "
                f"Cancelled `{cancelled_count}` active prompt(s), cleared debug/thinking toggles, "
                "and refreshed the cached system prompt for the next request."
            ),
            ephemeral=True,
        )

    benchmark_group = app_commands.Group(name="benchmark", description="Run benchmark tasks")

    @benchmark_group.command(name="earnings", description="Benchmark a no-context earnings comparison.")
    async def benchmark_earnings(interaction: discord.Interaction) -> None:
        if interaction.channel is None or interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        channel_id = getattr(interaction.channel, "id", None)
        if channel_id is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        request_key = (channel_id, interaction.user.id)
        if bot._active_requests.has_active(request_key):
            await interaction.response.send_message(
                "You already have an active request in this channel. Use `/cancel_all` to cancel active prompts.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        request_started_at = time.perf_counter()
        show_think_enabled = interaction.user.id in bot._thinking_enabled_users
        task = bot._active_requests.start(
            request_key,
            bot._generate_reply(
                guild_id=interaction.guild.id if interaction.guild else None,
                channel_id=channel_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                user_global_name=interaction.user.global_name or interaction.user.name,
                prompt="Compare the latest NVIDIA and AMD earnings reports. Focus on revenue, EPS, and guidance.",
                context_lines=[],
                image_attachment_urls=[],
                source_message_id=None,
                collect_latency_debug=True,
                show_think_enabled=show_think_enabled,
                search_requested=True,
                include_memories=False,
            ),
        )
        try:
            reply, metrics = await task
        except asyncio.CancelledError:
            await interaction.followup.send("Cancelled your active request.", ephemeral=True)
            return
        finally:
            bot._active_requests.clear(request_key, task)
        metrics = metrics or {}
        metrics["context_fetch_ms"] = 0
        metrics["end_to_end_ms"] = bot._elapsed_ms(request_started_at)
        reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
        reply = bot._render_discord_emojis(reply, interaction.guild)
        await bot._send_interaction_reply_chunks(interaction, reply, ephemeral=True)

    bot.tree.add_command(benchmark_group, guild=guild)
