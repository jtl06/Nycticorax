from __future__ import annotations

import re
from datetime import datetime, timezone

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    class _DiscordStub:
        class Message:  # type: ignore[empty-body]
            pass

        class Guild:  # type: ignore[empty-body]
            pass

        class Client:  # type: ignore[empty-body]
            pass

        class NotFound(Exception):
            pass

        class Forbidden(Exception):
            pass

        class HTTPException(Exception):
            pass

        class abc:
            class Messageable:  # type: ignore[empty-body]
                pass

    discord = _DiscordStub()

from nycti.formatting import extract_image_attachment_urls, parse_discord_message_links

TEXT_TRIGGER_RE = re.compile(r"(?<![A-Za-z0-9_])nycti(?![A-Za-z0-9_])(?:[,:;!?-]+)?", re.IGNORECASE)
DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT = 280
EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT = 560


def clean_trigger_content(message: discord.Message, *, bot_user_id: int | None) -> str:
    tokens = [token for token in message.content.split() if token not in _mention_tokens(bot_user_id)]
    content = " ".join(tokens).strip()
    content = TEXT_TRIGGER_RE.sub(" ", content)
    content = expand_user_mentions(content, getattr(message, "mentions", []))
    return " ".join(content.split()).strip()


def contains_named_trigger(text: str) -> bool:
    return bool(TEXT_TRIGGER_RE.search(text))


def message_has_visible_content(message: discord.Message) -> bool:
    return bool(message.content.strip() or message.attachments)


def format_message_line(
    message: discord.Message,
    *,
    prefix: str | None = None,
    include_timestamp: bool = False,
    content_char_limit: int = DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
) -> str:
    content = expand_user_mentions(" ".join(message.content.split()), getattr(message, "mentions", []))
    if not content and message.attachments:
        content = f"[{len(message.attachments)} attachment(s)]"
    effective_limit = max(content_char_limit, 16)
    if len(content) > effective_limit:
        content = f"{content[: max(effective_limit - 3, 1)]}..."
    label = f"[{prefix}] " if prefix else ""
    timestamp = _format_message_timestamp(message) if include_timestamp else ""
    timestamp_label = f"[{timestamp}] " if timestamp else ""
    return f"{label}{timestamp_label}{message.author.display_name}: {content}"


def expand_user_mentions(text: str, mentions: list[object] | tuple[object, ...]) -> str:
    expanded = text
    for user in mentions:
        user_id = getattr(user, "id", None)
        if user_id is None:
            continue
        label = _mention_label(user)
        replacement = f"@{label} (user_id={user_id})"
        expanded = re.sub(rf"<@!?{re.escape(str(user_id))}>", replacement, expanded)
    return expanded


def dedupe_lines(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped


def image_refs_for_message(
    message: discord.Message,
    *,
    label: str,
    image_limit: int,
) -> list[tuple[str, str]]:
    return [
        (f"{label} from {message.author.display_name}", url)
        for url in extract_image_attachment_urls(message.attachments, limit=image_limit)
    ]


def dedupe_image_refs(image_refs: list[tuple[str, str]], *, max_count: int) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for label, url in image_refs:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append((label, url))
        if len(deduped) >= max_count:
            break
    return deduped


async def fetch_older_context_lines(
    channel: discord.abc.Messageable,
    *,
    before: discord.Message,
    recent_limit: int,
    limit: int,
    content_char_limit: int = DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
) -> list[str]:
    if limit <= 0:
        return []
    history: list[discord.Message] = []
    async for item in channel.history(
        limit=limit + recent_limit,
        before=before,
        oldest_first=False,
    ):
        history.append(item)
    history.reverse()
    if len(history) <= recent_limit:
        return []
    older_messages = history[: -recent_limit][-limit:]
    return [
        format_message_line(item, include_timestamp=True, content_char_limit=content_char_limit)
        for item in older_messages
        if message_has_visible_content(item)
    ]


class MessageContextCollector:
    def __init__(
        self,
        *,
        bot: discord.Client,
        channel_context_limit: int,
        max_reply_chain_depth: int,
        max_linked_message_count: int,
        max_context_image_count: int,
        anchor_context_per_side: int,
    ) -> None:
        self.bot = bot
        self.channel_context_limit = channel_context_limit
        self.max_reply_chain_depth = max_reply_chain_depth
        self.max_linked_message_count = max_linked_message_count
        self.max_context_image_count = max_context_image_count
        self.anchor_context_per_side = anchor_context_per_side

    async def build_message_context(
        self,
        message: discord.Message,
    ) -> tuple[list[str], list[str], list[str]]:
        history_messages = await self._fetch_context_messages(
            message.channel,
            before=message,
        )
        history_lines = [
            format_message_line(item, include_timestamp=True)
            for item in history_messages
            if message_has_visible_content(item)
        ]
        if message_has_visible_content(message):
            history_lines.append(format_message_line(message, include_timestamp=True))
        reply_chain_messages = await self._collect_reply_chain_messages(message)
        reply_lines = [
            format_message_line(
                item,
                prefix=f"reply depth {depth}",
                include_timestamp=True,
            )
            for depth, item in enumerate(reply_chain_messages, start=1)
            if message_has_visible_content(item)
        ]
        linked_messages = await self._collect_linked_messages(
            message,
            reply_chain_messages=reply_chain_messages,
        )
        linked_lines = [
            format_message_line(item, prefix="linked message", include_timestamp=True)
            for item in linked_messages
            if message_has_visible_content(item)
        ]
        anchor_context_messages = await self._collect_anchor_context_messages(
            message,
            anchor_messages=[*reply_chain_messages, *linked_messages],
        )
        anchor_context_lines = [
            format_message_line(item, prefix="anchor context", include_timestamp=True)
            for item in anchor_context_messages
            if message_has_visible_content(item)
        ]
        context_lines = self._compose_context_lines(
            reply_lines=reply_lines,
            linked_lines=linked_lines,
            anchor_context_lines=anchor_context_lines,
            history_lines=history_lines,
        )
        image_refs: list[tuple[str, str]] = []
        image_refs.extend(
            image_refs_for_message(
                message,
                label="current message",
                image_limit=self.max_context_image_count,
            )
        )
        for depth, item in enumerate(reply_chain_messages, start=1):
            image_refs.extend(
                image_refs_for_message(
                    item,
                    label=f"reply depth {depth}",
                    image_limit=self.max_context_image_count,
                )
            )
        for item in linked_messages:
            image_refs.extend(
                image_refs_for_message(
                    item,
                    label="linked message",
                    image_limit=self.max_context_image_count,
                )
            )
        for item in history_messages:
            image_refs.extend(
                image_refs_for_message(
                    item,
                    label="recent context",
                    image_limit=self.max_context_image_count,
                )
            )
        for item in anchor_context_messages:
            image_refs.extend(
                image_refs_for_message(
                    item,
                    label="anchor context",
                    image_limit=self.max_context_image_count,
                )
            )
        deduped_image_refs = dedupe_image_refs(
            image_refs,
            max_count=self.max_context_image_count,
        )
        image_urls = [url for _, url in deduped_image_refs]
        image_context_lines = [
            f"- image {index}: {label}"
            for index, (label, _) in enumerate(deduped_image_refs, start=1)
        ]
        return context_lines, image_urls, image_context_lines

    def _compose_context_lines(
        self,
        *,
        reply_lines: list[str],
        linked_lines: list[str],
        anchor_context_lines: list[str],
        history_lines: list[str],
    ) -> list[str]:
        # Keep direct anchors and their nearby context, while always reserving room for recent channel lines.
        direct_lines = dedupe_lines(reply_lines + linked_lines)
        nearby_anchor_lines = dedupe_lines(anchor_context_lines)
        recent_history = dedupe_lines(history_lines)
        reserve_for_recent = 1 if recent_history else 0
        pinned_budget = max(self.channel_context_limit - reserve_for_recent, 0)

        if nearby_anchor_lines and pinned_budget > 1:
            direct_budget = min(len(direct_lines), pinned_budget - 1)
        else:
            direct_budget = min(len(direct_lines), pinned_budget)

        selected_direct = direct_lines[:direct_budget]
        selected_anchor_nearby = nearby_anchor_lines[: max(pinned_budget - len(selected_direct), 0)]
        pinned_lines = dedupe_lines(selected_direct + selected_anchor_nearby)
        remaining_budget = self.channel_context_limit - len(pinned_lines)
        selected_recent_history = recent_history[-remaining_budget:] if remaining_budget > 0 else []
        return dedupe_lines(pinned_lines + selected_recent_history)

    async def build_extended_history_context(
        self,
        message: discord.Message,
        *,
        limit: int,
    ) -> list[str]:
        if limit <= 0:
            return []
        return await fetch_older_context_lines(
            message.channel,
            before=message,
            recent_limit=self.channel_context_limit,
            limit=limit,
        )

    async def _fetch_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        history: list[discord.Message] = []
        async for item in channel.history(
            limit=self.channel_context_limit,
            before=before,
            oldest_first=False,
        ):
            history.append(item)
        history.reverse()
        return history

    async def _fetch_extended_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message,
        limit: int,
    ) -> list[discord.Message]:
        history: list[discord.Message] = []
        fetch_limit = limit + self.channel_context_limit
        async for item in channel.history(
            limit=fetch_limit,
            before=before,
            oldest_first=False,
        ):
            history.append(item)
        history.reverse()
        if len(history) <= self.channel_context_limit:
            return []
        return history[: -self.channel_context_limit][-limit:]

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
        history = getattr(channel, "history", None)
        if history is None:
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
        search_messages = [message, *reply_chain_messages]
        for source in search_messages:
            links = parse_discord_message_links(
                source.content,
                guild_id=message.guild.id if message.guild else None,
            )
            for channel_id, message_id in links:
                resolved = await self._fetch_linked_message(
                    guild=message.guild,
                    fallback_channel=message.channel,
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
        if message.reference is None or message.reference.message_id is None:
            return None
        referenced = message.reference.resolved
        if isinstance(referenced, discord.Message):
            return referenced
        try:
            return await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _fetch_linked_message(
        self,
        *,
        guild: discord.Guild | None,
        fallback_channel: discord.abc.Messageable,
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        channel: discord.abc.Messageable | None
        if getattr(fallback_channel, "id", None) == channel_id:
            channel = fallback_channel
        else:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return None
        if guild is not None and getattr(channel, "guild", None) not in (None, guild):
            return None
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            return await fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None


def _mention_tokens(bot_user_id: int | None) -> set[str]:
    if bot_user_id is None:
        return set()
    return {f"<@{bot_user_id}>", f"<@!{bot_user_id}>"}


def _mention_label(user: object) -> str:
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(user, attribute, None)
        if value:
            return str(value)
    return str(getattr(user, "id", "unknown"))


def _format_message_timestamp(message: discord.Message) -> str:
    created_at = getattr(message, "created_at", None)
    if not isinstance(created_at, datetime):
        return ""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
