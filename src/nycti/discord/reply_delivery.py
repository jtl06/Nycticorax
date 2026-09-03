from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging
import re

import discord

from nycti.discord.progress import DiscordResponseProgress
from nycti.discord.rate_limits import DISCORD_OUTBOUND_CIRCUIT_BREAKER
from nycti.formatting import (
    normalize_discord_math,
    normalize_discord_tables,
    split_message_chunks,
)
from nycti.table_images import extract_markdown_tables_as_images


LOGGER = logging.getLogger(__name__)
DISCORD_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


@dataclass(frozen=True, slots=True)
class DiscordReplyDelivery:
    messages: tuple[discord.Message, ...]
    complete: bool


async def send_message_reply_chunks(
    bot: object,
    message: discord.Message,
    text: str,
    *,
    progress_message: discord.Message | None = None,
    progress: DiscordResponseProgress | None = None,
) -> DiscordReplyDelivery:
    if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
        LOGGER.warning(
            "Skipping final Discord reply because the outbound rate-limit cooldown is active."
        )
        return DiscordReplyDelivery((), complete=False)
    text = normalize_discord_math(text)
    table_extraction = extract_markdown_tables_as_images(text)
    text = table_extraction.text or text
    if not table_extraction.images:
        text = normalize_discord_tables(text)
    chunks = split_message_chunks(text)
    bot_user_id = getattr(getattr(bot, "user", None), "id", None)
    user_mention_ids = {
        int(match.group(1))
        for match in DISCORD_USER_MENTION_RE.finditer(text)
        if int(match.group(1)) != bot_user_id
    }
    allowed_mentions = (
        discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=[discord.Object(id=user_id) for user_id in sorted(user_mention_ids)],
            replied_user=False,
        )
        if user_mention_ids
        else discord.AllowedMentions.none()
    )
    files = [
        discord.File(BytesIO(image.data), filename=image.filename)
        for image in table_extraction.images
    ]
    if progress_message is not None and (files or user_mention_ids):
        try:
            await progress_message.delete()
            if progress is not None:
                progress.mark_resolved()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                return DiscordReplyDelivery((), complete=False)
        progress_message = None
    if not chunks:
        if progress_message is not None:
            try:
                edited = await progress_message.edit(
                    content=text,
                    allowed_mentions=allowed_mentions,
                )
                if progress is not None:
                    progress.mark_resolved()
                return DiscordReplyDelivery((edited,), complete=True)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
                if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                    return DiscordReplyDelivery((), complete=False)
        try:
            sent = await message.reply(
                text,
                mention_author=False,
                files=files,
                allowed_mentions=allowed_mentions,
            )
        except discord.HTTPException as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                return DiscordReplyDelivery((), complete=False)
            raise
        return DiscordReplyDelivery((sent,), complete=True)
    if progress_message is not None:
        try:
            edited = await progress_message.edit(
                content=chunks[0],
                allowed_mentions=allowed_mentions,
            )
            if progress is not None:
                progress.mark_resolved()
            sent_messages = [edited]
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                return DiscordReplyDelivery((), complete=False)
            try:
                sent = await message.reply(
                    chunks[0],
                    mention_author=False,
                    files=files,
                    allowed_mentions=allowed_mentions,
                )
            except discord.HTTPException as reply_exc:
                if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(reply_exc):
                    return DiscordReplyDelivery((), complete=False)
                raise
            sent_messages = [sent]
    else:
        try:
            sent = await message.reply(
                chunks[0],
                mention_author=False,
                files=files,
                allowed_mentions=allowed_mentions,
            )
        except discord.HTTPException as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                return DiscordReplyDelivery((), complete=False)
            raise
        sent_messages = [sent]
    complete = True
    for chunk in chunks[1:]:
        if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
            complete = False
            break
        try:
            sent = await message.channel.send(
                chunk,
                allowed_mentions=allowed_mentions,
            )
        except discord.HTTPException as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                complete = False
                break
            raise
        sent_messages.append(sent)
    return DiscordReplyDelivery(tuple(sent_messages), complete=complete)


async def send_interaction_reply_chunks(
    interaction: discord.Interaction,
    text: str,
    *,
    ephemeral: bool = False,
) -> None:
    if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
        return
    chunks = split_message_chunks(text)
    allowed_mentions = discord.AllowedMentions.none()
    if not chunks:
        try:
            await interaction.followup.send(
                text,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        except discord.HTTPException as exc:
            if not DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                raise
        return
    try:
        await interaction.followup.send(
            chunks[0],
            ephemeral=ephemeral,
            allowed_mentions=allowed_mentions,
        )
    except discord.HTTPException as exc:
        if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
            return
        raise
    for chunk in chunks[1:]:
        if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
            break
        try:
            await interaction.followup.send(
                chunk,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        except discord.HTTPException as exc:
            if DISCORD_OUTBOUND_CIRCUIT_BREAKER.record_exception(exc):
                break
            raise
