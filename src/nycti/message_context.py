from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import urlparse

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
DEFAULT_RECENT_CONTEXT_MAX_AGE = timedelta(hours=24)


def clean_trigger_content(message: discord.Message, *, bot_user_id: int | None) -> str:
    tokens = [token for token in message.content.split() if token not in _mention_tokens(bot_user_id)]
    content = " ".join(tokens).strip()
    content = TEXT_TRIGGER_RE.sub(" ", content)
    content = expand_user_mentions(content, getattr(message, "mentions", []))
    return " ".join(content.split()).strip()


def message_has_visible_content(message: discord.Message) -> bool:
    return bool(message.content.strip() or message.attachments or getattr(message, "embeds", []))


def format_message_line(
    message: discord.Message,
    *,
    prefix: str | None = None,
    include_timestamp: bool = False,
    content_char_limit: int = DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
) -> str:
    content = expand_user_mentions(" ".join(message.content.split()), getattr(message, "mentions", []))
    embed_preview = _format_embed_preview(message)
    if not content and message.attachments:
        content = f"[{len(message.attachments)} attachment(s)]"
    if embed_preview:
        if content:
            content = f"{content} [embed: {embed_preview}]"
        else:
            content = f"[embed: {embed_preview}]"
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
            format_message_line(item)
            for item in history_messages
            if message_has_visible_content(item)
            and _is_within_recent_context_window(item, reference=message)
        ]
        if message_has_visible_content(message):
            history_lines.append(format_message_line(message))
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

    async def _fetch_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        if self.channel_context_limit <= 0:
            return []
        cached_messages = self._cached_context_messages(channel, before=before)
        if cached_messages:
            return cached_messages
        history: list[discord.Message] = []
        async for item in channel.history(
            limit=self.channel_context_limit,
            before=before,
            oldest_first=False,
        ):
            history.append(item)
        history.reverse()
        return history

    def _cached_context_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        before: discord.Message | None,
    ) -> list[discord.Message]:
        cached_messages = getattr(self.bot, "cached_messages", ()) or ()
        candidates = [
            cast(discord.Message, item)
            for item in cached_messages
            if _message_matches_channel(item, channel)
            and _message_precedes(item, before)
            and (before is None or _is_within_recent_context_window(item, reference=before))
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
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        message_id = reference.message_id
        expected_guild = getattr(message, "guild", None)
        for candidate in (
            getattr(reference, "resolved", None),
            getattr(reference, "cached_message", None),
        ):
            if _message_matches_target(
                candidate,
                message_id=message_id,
                channel=message.channel,
                guild=expected_guild,
            ):
                return cast(discord.Message, candidate)
        cached = self._get_cached_message(
            message_id,
            channel=message.channel,
            guild=expected_guild,
        )
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
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        cached = self._get_cached_message(
            message_id,
            channel_id=channel_id,
            guild=guild,
        )
        if cached is not None:
            return cached

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
        if guild is not None and getattr(channel, "guild", None) not in (None, guild):
            return None
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


def _is_within_recent_context_window(
    message: discord.Message,
    *,
    reference: discord.Message,
) -> bool:
    created_at = _message_created_at(message)
    reference_created_at = _message_created_at(reference)
    if created_at is None or reference_created_at is None:
        return True
    return created_at >= reference_created_at - DEFAULT_RECENT_CONTEXT_MAX_AGE


def _message_created_at(message: discord.Message) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


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
    message_time = _message_created_at(cast(discord.Message, message))
    before_time = _message_created_at(before)
    return message_time is not None and before_time is not None and message_time < before_time


def _message_order_key(message: discord.Message) -> tuple[datetime, int]:
    created_at = _message_created_at(message) or datetime.min.replace(tzinfo=timezone.utc)
    message_id = getattr(message, "id", 0)
    return created_at, message_id if isinstance(message_id, int) else 0


def _format_embed_preview(message: discord.Message, *, max_embeds: int = 2, max_chars: int = 180) -> str:
    embeds = list(getattr(message, "embeds", []) or [])
    if not embeds:
        return ""
    previews: list[str] = []
    for embed in embeds[:max_embeds]:
        preview = _format_single_embed_preview(embed)
        if preview:
            previews.append(preview)
    if not previews:
        return ""
    joined = " | ".join(previews)
    if len(joined) > max_chars:
        return joined[: max_chars - 3].rstrip() + "..."
    return joined


def _format_single_embed_preview(embed: object) -> str:
    title = _normalize_embed_text(getattr(embed, "title", None))
    description = _normalize_embed_text(getattr(embed, "description", None))
    provider = _normalize_embed_text(getattr(getattr(embed, "provider", None), "name", None))
    author = _normalize_embed_text(getattr(getattr(embed, "author", None), "name", None))
    embed_url = _normalize_embed_text(getattr(embed, "url", None))

    header_parts = [part for part in (provider or _embed_domain(embed_url), author) if part]
    body_parts = [part for part in (title, description) if part]
    if header_parts and body_parts:
        text = f"{' - '.join(header_parts)}: {' — '.join(body_parts)}"
    elif body_parts:
        text = " — ".join(body_parts)
    elif header_parts:
        text = " - ".join(header_parts)
    else:
        return ""
    if len(text) > 120:
        return text[:117].rstrip() + "..."
    return text


def _normalize_embed_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _embed_domain(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain
