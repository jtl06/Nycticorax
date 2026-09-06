from __future__ import annotations

import asyncio
from typing import Any

import httpx

from nycti.chat.action_confirmation import ActionProposal, EmojiImportAction
from nycti.emoji_catalog import ObservedEmoji

MAX_EMOJI_BYTES = 256 * 1024


def can_create_emoji(member: Any) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and (
        getattr(permissions, "create_expressions", False)
        or getattr(permissions, "manage_expressions", False)
        or getattr(permissions, "administrator", False)
    ))


async def fetch_emoji_image(payload: EmojiImportAction) -> bytes:
    suffix = "gif" if payload.animated else "png"
    # Only an exact Discord CDN asset is accepted, never a user-supplied URL.
    url = f"https://cdn.discordapp.com/emojis/{payload.source_id}.{suffix}?size=128&quality=lossless"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_EMOJI_BYTES:
                    raise ValueError("Emoji image exceeds Discord's 256 KiB limit.")
    if not content.startswith((b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")):
        raise ValueError("Discord did not return a PNG or GIF emoji image.")
    return bytes(content)


async def execute_emoji_import(bot: Any, proposal: ActionProposal) -> str:
    payload = proposal.payload
    if not isinstance(payload, EmojiImportAction):
        raise ValueError("Emoji import payload mismatch.")
    guild = bot.get_guild(proposal.guild_id)
    if guild is None:
        return "Emoji import stopped: server unavailable."
    member = await guild.fetch_member(proposal.user_id)
    me = await guild.fetch_member(bot.user.id)
    channel = guild.get_channel_or_thread(proposal.request_channel_id)
    if channel is None or not channel.permissions_for(member).view_channel:
        return "Emoji import stopped: the requester can no longer view this channel."
    if not can_create_emoji(member) or not can_create_emoji(me):
        return "Emoji import needs Create Expressions or Manage Expressions for both you and Nycti."
    # Serialize uploads so two separately confirmed proposals cannot duplicate an asset.
    if not hasattr(bot, "_emoji_import_lock"):
        bot._emoji_import_lock = asyncio.Lock()
    async with bot._emoji_import_lock:
        catalog = bot.emoji_catalog
        await catalog.load(guild.id)
        emojis = await guild.fetch_emojis()
        existing_id = catalog.imported_id(guild.id, payload.source_id) or payload.source_id
        existing = next((emoji for emoji in emojis if emoji.id == existing_id), None)
        if existing is not None:
            return f"Already available: {existing}"
        if any(emoji.name.casefold() == payload.name.casefold() for emoji in emojis):
            return "That emoji name already exists. Choose another name; nothing was replaced."
        if sum(emoji.animated == payload.animated for emoji in emojis) >= guild.emoji_limit:
            return "No free server slots for this emoji type. Nothing was removed."
        image = await fetch_emoji_image(payload)
        created = await guild.create_custom_emoji(
            name=payload.name, image=image,
            reason=f"Nycti confirmed emoji import {proposal.proposal_id} by {proposal.user_id}",
        )
        await catalog.observe(guild.id, ObservedEmoji(payload.source_id, payload.name, payload.animated).token)
        await catalog.record_import(guild.id, payload.source_id, created.id)
        return f"Imported {created}. Nycti can now use :{created.name}:."
