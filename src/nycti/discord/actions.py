from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.chat.action_confirmation import ActionConfirmationError
from nycti.discord.common import SERVER_ONLY_MESSAGE

LOGGER = logging.getLogger(__name__)
CONFIRMATION_TIMEOUT_MESSAGE = (
    "Confirmation timed out, so the action's final status is unknown. Check the target channel or "
    "your reminders before requesting another action."
)
CONFIRMATION_ERROR_MESSAGE = (
    "Nycti could not verify the confirmed action's final status. It may have completed; check the "
    "target channel or your reminders before retrying."
)


def normalize_proposal_id(value: str) -> str:
    normalized = value.strip().strip("`")
    if normalized.casefold().startswith("proposal:"):
        normalized = normalized.split(":", 1)[1].strip()
    return normalized


async def confirm_action_proposal(
    bot: Any,
    *,
    proposal_id: str,
    guild_id: int,
    channel_id: int,
    user_id: int,
) -> str:
    executor = bot._chat_orchestrator.tool_runner.executor
    return await asyncio.wait_for(
        executor.confirm_action(
            normalize_proposal_id(proposal_id),
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        ),
        timeout=15.0,
    )


def format_confirmation_failure(exc: Exception) -> str:
    if isinstance(exc, ActionConfirmationError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return CONFIRMATION_TIMEOUT_MESSAGE
    return CONFIRMATION_ERROR_MESSAGE


def register_action_commands(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(
        name="confirm",
        description="Confirm one exact Nycti action proposal.",
        guild=guild,
    )
    @app_commands.describe(proposal="Proposal ID shown by Nycti, such as act_abc123")
    async def confirm(interaction: discord.Interaction, proposal: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await confirm_action_proposal(
                bot,
                proposal_id=proposal,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=interaction.user.id,
            )
        except Exception as exc:
            result = format_confirmation_failure(exc)
            if not isinstance(exc, ActionConfirmationError):
                LOGGER.exception("Confirmed action did not return a definitive receipt.")
        await interaction.followup.send(
            result,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
