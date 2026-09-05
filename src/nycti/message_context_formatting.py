from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    class _DiscordStub:
        class Message:  # type: ignore[empty-body]
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

from nycti.discord.invocation import strip_explicit_name_prefix
from nycti.formatting import extract_image_attachment_urls


DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT = 280
EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT = 560
DEFAULT_RECENT_CONTEXT_MAX_AGE = timedelta(hours=24)


def clean_trigger_content(
    message: discord.Message,
    *,
    bot_user_id: int | None,
    invocation_name: str = "Nycti",
    strip_invocation_name: bool = True,
) -> str:
    content = message.content
    if bot_user_id is not None:
        content = re.sub(
            rf"<@!?{re.escape(str(bot_user_id))}>(?:\s*[,;:!?\-—]+)?",
            " ",
            content,
        )
    if strip_invocation_name:
        content = strip_explicit_name_prefix(content, invocation_name=invocation_name)
    content = " ".join(content.split()).strip()
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
        content = f"{content} [embed: {embed_preview}]" if content else f"[embed: {embed_preview}]"
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
        replacement = f"@{_mention_label(user)} (user_id={user_id})"
        expanded = re.sub(rf"<@!?{re.escape(str(user_id))}>", replacement, expanded)
    return expanded


def dedupe_lines(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(lines))


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


def collect_message_members(messages: list[object]) -> list[object]:
    members: list[object] = []
    seen_user_ids: set[int] = set()
    for message in messages:
        for member in [getattr(message, "author", None), *getattr(message, "mentions", [])]:
            user_id = getattr(member, "id", None)
            if not isinstance(user_id, int) or user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            members.append(member)
    return members


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
    return [
        format_message_line(item, include_timestamp=True, content_char_limit=content_char_limit)
        for item in history[: -recent_limit][-limit:]
        if message_has_visible_content(item)
    ]


def is_within_recent_context_window(
    message: discord.Message,
    *,
    reference: discord.Message,
) -> bool:
    created_at = message_created_at(message)
    reference_created_at = message_created_at(reference)
    if created_at is None or reference_created_at is None:
        return True
    return created_at >= reference_created_at - DEFAULT_RECENT_CONTEXT_MAX_AGE


def message_created_at(message: discord.Message) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


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


def _format_embed_preview(message: discord.Message, *, max_embeds: int = 2, max_chars: int = 180) -> str:
    previews = [
        preview
        for preview in (
            _format_single_embed_preview(embed)
            for embed in list(getattr(message, "embeds", []) or [])[:max_embeds]
        )
        if preview
    ]
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
    return text[:117].rstrip() + "..." if len(text) > 120 else text


def _normalize_embed_text(value: object) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def _embed_domain(url: str) -> str:
    if not url:
        return ""
    domain = urlparse(url).netloc.lower()
    return domain[4:] if domain.startswith("www.") else domain
