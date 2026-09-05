from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from nycti.discord.channel_access import member_can_view_channel
from nycti.formatting import parse_discord_message_links
from nycti.message_context_formatting import (
    discord,
    is_within_recent_context_window,
    message_created_at,
    message_has_visible_content,
)


class DiscordMessageContextSource:
    """Fetch and permission-check Discord messages used as prompt context."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        channel_context_limit: int,
        max_reply_chain_depth: int,
        max_linked_message_count: int,
        anchor_context_per_side: int,
    ) -> None:
        self.bot = bot
        self.channel_context_limit = channel_context_limit
        self.max_reply_chain_depth = max_reply_chain_depth
        self.max_linked_message_count = max_linked_message_count
        self.anchor_context_per_side = anchor_context_per_side

    async def _fetch_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        if self.channel_context_limit <= 0:
            return []
        cached_messages = self._cached_context_messages(channel, before=before)
        if len(cached_messages) >= self.channel_context_limit:
            return cached_messages
        history: list[discord.Message] = []
        try:
            async for item in channel.history(
                limit=self.channel_context_limit,
                before=before,
                oldest_first=False,
            ):
                history.append(item)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
            pass
        history.reverse()
        messages_by_id = {item.id: item for item in history}
        messages_by_id.update({item.id: item for item in cached_messages})
        merged_messages = sorted(messages_by_id.values(), key=_message_order_key)
        return merged_messages[-self.channel_context_limit :]

    def _cached_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        candidates = [
            cast(discord.Message, item)
            for item in getattr(self.bot, "cached_messages", ()) or ()
            if _message_matches_channel(item, channel)
            and _message_precedes(item, before)
            and (before is None or is_within_recent_context_window(item, reference=before))
            and message_has_visible_content(item)
        ]
        candidates.sort(key=_message_order_key)
        return candidates[-self.channel_context_limit :]

    async def _collect_reply_chain_messages(self, message: discord.Message) -> list[discord.Message]:
        chain: list[discord.Message] = []
        seen_ids: set[int] = set()
        current = message
        for _ in range(self.max_reply_chain_depth):
            referenced = await self._resolve_referenced_message(current)
            if referenced is None or referenced.id in seen_ids:
                break
            seen_ids.add(referenced.id)
            chain.append(referenced)
            current = referenced
        return chain

    async def _collect_anchor_context_messages(
        self,
        message: discord.Message,
        *,
        anchor_messages: list[discord.Message],
    ) -> list[discord.Message]:
        if self.anchor_context_per_side <= 0:
            return []
        nearby_messages: list[discord.Message] = []
        seen_ids = {message.id, *(item.id for item in anchor_messages)}
        for anchor in anchor_messages:
            before_messages, after_messages = await self._fetch_anchor_neighbors(
                anchor,
                fallback_channel=message.channel,
            )
            for nearby in [*before_messages, *after_messages]:
                if nearby.id in seen_ids:
                    continue
                seen_ids.add(nearby.id)
                nearby_messages.append(nearby)
        return nearby_messages

    async def _fetch_anchor_neighbors(
        self,
        anchor: discord.Message,
        *,
        fallback_channel: discord.abc.Messageable,
    ) -> tuple[list[discord.Message], list[discord.Message]]:
        channel = getattr(anchor, "channel", None) or fallback_channel
        if getattr(channel, "history", None) is None:
            return [], []
        before_messages: list[discord.Message] = []
        after_messages: list[discord.Message] = []
        try:
            async for item in channel.history(
                limit=self.anchor_context_per_side,
                before=anchor,
                oldest_first=False,
            ):
                before_messages.append(item)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
            before_messages = []
        before_messages.reverse()
        try:
            async for item in channel.history(
                limit=self.anchor_context_per_side,
                after=anchor,
                oldest_first=True,
            ):
                after_messages.append(item)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
            after_messages = []
        return before_messages, after_messages

    async def _collect_linked_messages(
        self,
        message: discord.Message,
        *,
        reply_chain_messages: list[discord.Message],
    ) -> list[discord.Message]:
        linked_messages: list[discord.Message] = []
        seen_ids = {message.id, *(item.id for item in reply_chain_messages)}
        for source in [message, *reply_chain_messages]:
            links = parse_discord_message_links(
                source.content,
                guild_id=message.guild.id if message.guild else None,
            )
            for channel_id, message_id in links:
                resolved = await self._fetch_linked_message(
                    guild=message.guild,
                    fallback_channel=message.channel,
                    requester=message.author,
                    channel_id=channel_id,
                    message_id=message_id,
                )
                if resolved is None or resolved.id in seen_ids:
                    continue
                seen_ids.add(resolved.id)
                linked_messages.append(resolved)
                if len(linked_messages) >= self.max_linked_message_count:
                    return linked_messages
        return linked_messages

    async def _resolve_referenced_message(self, message: discord.Message) -> discord.Message | None:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        message_id = reference.message_id
        expected_guild = getattr(message, "guild", None)
        for candidate in (getattr(reference, "resolved", None), getattr(reference, "cached_message", None)):
            if _message_matches_target(
                candidate,
                message_id=message_id,
                channel=message.channel,
                guild=expected_guild,
            ):
                return cast(discord.Message, candidate)
        cached = self._get_cached_message(message_id, channel=message.channel, guild=expected_guild)
        if cached is not None:
            return cached
        fetch_message = getattr(message.channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            return await fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _fetch_linked_message(
        self,
        *,
        guild: discord.Guild | None,
        fallback_channel: discord.abc.Messageable,
        requester: object,
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        channel: discord.abc.Messageable | None
        if getattr(fallback_channel, "id", None) == channel_id:
            channel = fallback_channel
        else:
            get_channel = getattr(self.bot, "get_channel", None)
            channel = get_channel(channel_id) if get_channel is not None else None
            if channel is None:
                fetch_channel = getattr(self.bot, "fetch_channel", None)
                if fetch_channel is None:
                    return None
                try:
                    channel = await fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return None
        if channel is None:
            return None
        if guild is not None:
            expected_guild_id = getattr(guild, "id", None)
            actual_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if expected_guild_id is None or actual_guild_id != expected_guild_id:
                return None
        if not await member_can_view_channel(channel, requester):
            return None
        cached = self._get_cached_message(message_id, channel=channel, guild=guild)
        if cached is not None:
            return cached
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            return await fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _get_cached_message(
        self,
        message_id: int,
        *,
        channel: discord.abc.Messageable | None = None,
        channel_id: int | None = None,
        guild: discord.Guild | None = None,
    ) -> discord.Message | None:
        get_message = getattr(self.bot, "get_message", None)
        direct = get_message(message_id) if get_message is not None else None
        if _message_matches_target(
            direct,
            message_id=message_id,
            channel=channel,
            channel_id=channel_id,
            guild=guild,
        ):
            return cast(discord.Message, direct)
        for candidate in getattr(self.bot, "cached_messages", ()) or ():
            if _message_matches_target(
                candidate,
                message_id=message_id,
                channel=channel,
                channel_id=channel_id,
                guild=guild,
            ):
                return cast(discord.Message, candidate)
        return None


def _message_matches_channel(message: object, channel: object) -> bool:
    message_channel = getattr(message, "channel", None)
    message_channel_id = getattr(message_channel, "id", None)
    channel_id = getattr(channel, "id", None)
    if message_channel_id is not None and channel_id is not None:
        if message_channel_id != channel_id:
            return False
    elif message_channel is not channel:
        return False
    message_guild_id = getattr(getattr(message, "guild", None), "id", None)
    if message_guild_id is None:
        message_guild_id = getattr(getattr(message_channel, "guild", None), "id", None)
    channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
    return not (
        message_guild_id is not None
        and channel_guild_id is not None
        and message_guild_id != channel_guild_id
    )


def _message_matches_target(
    message: object,
    *,
    message_id: int,
    channel: object | None = None,
    channel_id: int | None = None,
    guild: object | None = None,
) -> bool:
    if message is None or getattr(message, "id", None) != message_id:
        return False
    message_channel = getattr(message, "channel", None)
    expected_channel_id = channel_id if channel_id is not None else getattr(channel, "id", None)
    actual_channel_id = getattr(message_channel, "id", None)
    if expected_channel_id is not None:
        if actual_channel_id != expected_channel_id:
            return False
    elif channel is not None and message_channel is not channel:
        return False
    expected_guild_id = getattr(guild, "id", None)
    actual_guild_id = getattr(getattr(message, "guild", None), "id", None)
    if actual_guild_id is None:
        actual_guild_id = getattr(getattr(message_channel, "guild", None), "id", None)
    return not (
        expected_guild_id is not None
        and actual_guild_id is not None
        and expected_guild_id != actual_guild_id
    )


def _message_precedes(message: object, before: discord.Message | None) -> bool:
    if before is None:
        return True
    message_id = getattr(message, "id", None)
    before_id = getattr(before, "id", None)
    if isinstance(message_id, int) and isinstance(before_id, int):
        return message_id < before_id
    message_time = message_created_at(cast(discord.Message, message))
    before_time = message_created_at(before)
    return message_time is not None and before_time is not None and message_time < before_time


def _message_order_key(message: discord.Message) -> tuple[datetime, int]:
    created_at = message_created_at(message) or datetime.min.replace(tzinfo=timezone.utc)
    message_id = getattr(message, "id", 0)
    return created_at, message_id if isinstance(message_id, int) else 0
