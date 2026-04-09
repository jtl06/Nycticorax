from __future__ import annotations

import re

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


def clean_trigger_content(message: discord.Message, *, bot_user_id: int | None) -> str:
    tokens = [token for token in message.content.split() if token not in _mention_tokens(bot_user_id)]
    content = " ".join(tokens).strip()
    content = TEXT_TRIGGER_RE.sub(" ", content)
    return " ".join(content.split()).strip()


def contains_named_trigger(text: str) -> bool:
    return bool(TEXT_TRIGGER_RE.search(text))


def message_has_visible_content(message: discord.Message) -> bool:
    return bool(message.content.strip() or message.attachments)


def format_message_line(message: discord.Message, *, prefix: str | None = None) -> str:
    content = " ".join(message.content.split())
    if not content and message.attachments:
        content = f"[{len(message.attachments)} attachment(s)]"
    if len(content) > 400:
        content = f"{content[:397]}..."
    label = f"[{prefix}] " if prefix else ""
    return f"{label}{message.author.display_name}: {content}"


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


class MessageContextCollector:
    def __init__(
        self,
        *,
        bot: discord.Client,
        channel_context_limit: int,
        max_reply_chain_depth: int,
        max_linked_message_count: int,
        max_context_image_count: int,
    ) -> None:
        self.bot = bot
        self.channel_context_limit = channel_context_limit
        self.max_reply_chain_depth = max_reply_chain_depth
        self.max_linked_message_count = max_linked_message_count
        self.max_context_image_count = max_context_image_count

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
        ]
        if message_has_visible_content(message):
            history_lines.append(format_message_line(message))
        reply_chain_messages = await self._collect_reply_chain_messages(message)
        reply_lines = [
            format_message_line(
                item,
                prefix=f"reply depth {depth}",
            )
            for depth, item in enumerate(reply_chain_messages, start=1)
            if message_has_visible_content(item)
        ]
        linked_messages = await self._collect_linked_messages(
            message,
            reply_chain_messages=reply_chain_messages,
        )
        linked_lines = [
            format_message_line(item, prefix="linked message")
            for item in linked_messages
            if message_has_visible_content(item)
        ]
        context_lines = dedupe_lines(reply_lines + linked_lines + history_lines)
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
