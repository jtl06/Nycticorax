from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

SEARCH_TRIGGER_PHRASE = "use search"


def format_ping_message(latency_seconds: float) -> str:
    latency_ms = round(max(latency_seconds, 0.0) * 1000)
    return f"Pong! `{latency_ms} ms`"


def format_help_message() -> str:
    return (
        "**Nycti Help**\n"
        "Triggers:\n"
        "- mention the bot\n"
        "- reply to a bot message\n"
        "- `/chat prompt:<text>`\n\n"
        "Core commands:\n"
        "- `/help`: show this help message\n"
        "- `/ping`: verify the bot is online\n"
        "- `/chat prompt:<text>`: ask something in the current channel\n"
        "- `/show debug enabled:<true|false>`: toggle latency/debug output for your own replies\n"
        "- `/show thinking enabled:<true|false>`: toggle reasoning summary visibility for your own replies\n"
        "- `/benchmark earnings`: run the built-in no-context earnings benchmark\n"
        "- `/cancel_all`: cancel all in-flight prompts (`Manage Server` required)\n"
        "- `/reset`: clear runtime state and active prompts (`Manage Server` required)\n\n"
        "Memory:\n"
        "- `/memories`: list your stored memories\n"
        "- `/forget memory_id:<id>`: delete one memory\n"
        "- `/memory on`: enable memory retrieval/storage\n"
        "- `/memory off`: disable memory retrieval/storage\n\n"
        "Reminders:\n"
        "- ask naturally in chat, for example: `@Nycti remind me on 2026-03-25 to check NVDA earnings`\n"
        "- `/reminders`: list your pending reminders\n"
        "- `/reminders_all`: list all pending reminders in this server (`Manage Server` required)\n"
        "- `/forget_reminder reminder_id:<id>`: delete one of your pending reminders\n"
        "- `/config time timezone:<zone>`: set your timezone, for example `PST` or `America/Los_Angeles`\n\n"
        "Channels / changelog:\n"
        "- `/config changelog channel:<channel>`: set or clear the startup changelog channel for this server (`Manage Server` required)\n"
        "- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)\n"
        "- `/channel delete alias:<name>`: remove an alias (`Manage Server` required)\n"
        "- `/channel list`: show configured aliases\n"
        "- `/test changelog`: post the current changelog message to the configured changelog channel (`Manage Server` required)\n\n"
        "Tips:\n"
        "- include `use search` to force at least one web search\n"
        "- the bot only posts in other channels when you explicitly ask it to and it has permission there\n"
        "- reminders and date parsing use your configured timezone\n"
        "- debug/thinking toggles are per-user and reset on bot restart"
    )


def format_latency_debug_block(metrics: Mapping[str, int | str]) -> str:
    ordered_keys = (
        "chat_model",
        "memory_model",
        "chat_prompt_tokens",
        "chat_completion_tokens",
        "chat_total_tokens",
        "end_to_end_ms",
        "context_fetch_ms",
        "memory_retrieval_ms",
        "tool_call_count",
        "web_search_query_count",
        "web_search_ms",
        "reminder_create_count",
        "reminder_create_ms",
        "channel_send_count",
        "channel_send_ms",
        "chat_llm_ms",
        "chat_usage_write_ms",
        "chat_commit_ms",
        "reply_generation_ms",
    )
    lines = ["latency_debug_ms"]
    for key in ordered_keys:
        if key in metrics:
            lines.append(f"{key}: {metrics[key]}")
    chat_llm_ms = int(metrics.get("chat_llm_ms", 0)) if "chat_llm_ms" in metrics else 0
    chat_total_tokens = int(metrics.get("chat_total_tokens", 0)) if "chat_total_tokens" in metrics else 0
    if chat_llm_ms > 0 and chat_total_tokens > 0:
        tokens_per_second = round(chat_total_tokens / (chat_llm_ms / 1000), 1)
        lines.append(f"chat_tokens_per_s: {tokens_per_second}")
    lines.append("memory_extraction: background")
    raw_tool_trace = str(metrics.get("raw_tool_trace", "")).strip() if "raw_tool_trace" in metrics else ""
    if raw_tool_trace:
        lines.append("")
        lines.append("raw_tool_trace")
        lines.append(raw_tool_trace)
    return "```text\n" + "\n".join(lines) + "\n```"


def extract_think_content(text: str) -> list[str]:
    blocks = re.findall(r"<think>(.*?)</think>", text, flags=re.IGNORECASE | re.DOTALL)
    return [block.strip() for block in blocks if block.strip()]


def format_thinking_block(reasoning_parts: list[str]) -> str:
    if not reasoning_parts:
        return ""
    combined = "\n\n".join(reasoning_parts)
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in combined.splitlines())
    return f"-# reasoning\n{quoted}"


def append_debug_block(reply_text: str, debug_block: str, limit: int | None = 1900) -> str:
    suffix = "\n\n" + debug_block
    if limit is None:
        return reply_text + suffix
    if len(reply_text) + len(suffix) <= limit:
        return reply_text + suffix
    trim_target = max(0, limit - len(suffix))
    if trim_target <= 3:
        return debug_block[:limit]
    trimmed = reply_text[: trim_target - 3].rstrip()
    return f"{trimmed}...{suffix}"


def split_message_chunks(text: str, limit: int = 1900) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return [""]
    chunks: list[str] = []
    current = ""
    for block in cleaned.split("\n\n"):
        piece = block.strip()
        if not piece:
            continue
        candidate = piece if not current else f"{current}\n\n{piece}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(piece) <= limit:
            current = piece
            continue
        line_chunks = _split_large_block(piece, limit)
        chunks.extend(line_chunks[:-1])
        current = line_chunks[-1]
    if current:
        chunks.append(current)
    return chunks or [cleaned[:limit]]


def normalize_discord_tables(text: str) -> str:
    lines = text.splitlines()
    normalized: list[str] = []
    index = 0
    while index < len(lines):
        if _looks_like_markdown_table_header(lines, index):
            table_lines = [lines[index]]
            index += 2
            while index < len(lines) and _looks_like_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            normalized.append(_render_discord_table_block(table_lines))
            continue
        normalized.append(lines[index])
        index += 1
    return "\n".join(normalized)


def strip_think_blocks(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def render_custom_emoji_aliases(text: str, replacements: Mapping[str, str]) -> str:
    if not replacements:
        return text

    def _replace(match: re.Match[str]) -> str:
        alias = match.group(1)
        return replacements.get(alias, match.group(0))

    return re.sub(r":([a-zA-Z0-9_]+):", _replace, text)


def format_current_datetime_context(now: datetime, timezone_name: str | None = None) -> str:
    local_now = now.astimezone(ZoneInfo(timezone_name)) if timezone_name else now.astimezone()
    timezone_name = local_now.tzname() or local_now.strftime("%z")
    return local_now.strftime(f"%Y-%m-%d %H:%M:%S {timezone_name}")


def format_discord_message_link(
    *,
    guild_id: int | None,
    channel_id: int,
    message_id: int,
) -> str:
    guild_segment = str(guild_id) if guild_id is not None else "@me"
    return f"https://discord.com/channels/{guild_segment}/{channel_id}/{message_id}"


def format_reminder_list(
    reminders: Iterable[object],
    *,
    timezone_name: str,
    include_owner: bool = False,
) -> str:
    rendered: list[str] = []
    tz = ZoneInfo(timezone_name)
    for reminder in reminders:
        remind_at = reminder.remind_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        line = f"`{reminder.id}` {remind_at} - {reminder.reminder_text}"
        if include_owner:
            line = f"`{reminder.id}` <@{reminder.user_id}> in <#{reminder.channel_id}> {remind_at} - {reminder.reminder_text}"
        if reminder.source_message_id is not None:
            jump_link = format_discord_message_link(
                guild_id=reminder.guild_id,
                channel_id=reminder.channel_id,
                message_id=reminder.source_message_id,
            )
            line += f" ({jump_link})"
        rendered.append(line)
    return "\n".join(rendered)


def format_channel_alias_list(aliases: Iterable[object]) -> str:
    rendered = [f"`{alias.alias}` -> <#{alias.channel_id}> (`{alias.channel_id}`)" for alias in aliases]
    return "\n".join(rendered) if rendered else "No channel aliases configured yet."


def parse_json_object_payload(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match is None:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def parse_query_list_payload(text: str, *, fallback: str, limit: int = 3) -> list[str]:
    payload = parse_json_object_payload(text)
    if payload is None:
        return _normalize_queries([], fallback=fallback, limit=limit)
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list):
        return _normalize_queries([], fallback=fallback, limit=limit)
    queries = [str(query).strip() for query in raw_queries]
    return _normalize_queries(queries, fallback=fallback, limit=limit)


def extract_search_query(text: str) -> tuple[bool, str]:
    return _extract_trigger_query(text, SEARCH_TRIGGER_PHRASE)


def _extract_trigger_query(text: str, phrase: str) -> tuple[bool, str]:
    normalized = " ".join(text.split())
    if not normalized:
        return False, ""
    escaped = re.escape(phrase)
    match = re.search(rf"\b{escaped}\b", normalized, flags=re.IGNORECASE)
    if match is None:
        return False, normalized
    query = re.sub(rf"\b{escaped}\b", "", normalized, count=1, flags=re.IGNORECASE).strip()
    query = " ".join(query.split())
    return True, query


def _normalize_queries(queries: list[str], *, fallback: str, limit: int) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = " ".join(query.split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
        if len(normalized) >= limit:
            break
    fallback_cleaned = " ".join(fallback.split())
    if not normalized and fallback_cleaned:
        return [fallback_cleaned]
    return normalized


def _split_large_block(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        piece = line if not current else f"{current}\n{line}"
        if len(piece) <= limit:
            current = piece
            continue
        if current:
            chunks.append(current)
            current = ""
        remaining = line
        while len(remaining) > limit:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        current = remaining
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def _looks_like_markdown_table_header(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if not _looks_like_table_row(header):
        return False
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?", separator))


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and not stripped.startswith("```")


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _render_discord_table_block(lines: list[str]) -> str:
    rows = [_split_table_cells(line) for line in lines]
    column_count = max((len(row) for row in rows), default=0)
    padded_rows = [row + [""] * (column_count - len(row)) for row in rows]
    widths = [max(len(row[column]) for row in padded_rows) for column in range(column_count)]

    rendered_lines: list[str] = []
    for row_index, row in enumerate(padded_rows):
        rendered_lines.append(" | ".join(row[column].ljust(widths[column]) for column in range(column_count)).rstrip())
        if row_index == 0:
            rendered_lines.append("-+-".join("-" * widths[column] for column in range(column_count)))
    return "```text\n" + "\n".join(rendered_lines).rstrip() + "\n```"
