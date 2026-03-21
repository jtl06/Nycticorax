from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from nycti.formatting import format_current_datetime_context

if TYPE_CHECKING:
    from nycti.channel_aliases import ChannelAliasService
    from nycti.memory.service import MemoryService


@dataclass(slots=True)
class PreparedChatContext:
    current_datetime_text: str
    memories_block: str
    channel_alias_block: str
    memory_enabled: bool
    retrieved_memories: list[object]
    memory_retrieval_ms: int


class ChatContextBuilder:
    def __init__(
        self,
        *,
        memory_service: Any,
        channel_alias_service: Any,
    ) -> None:
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service

    async def prepare(
        self,
        session,
        *,
        guild_id: int | None,
        user_id: int,
        prompt: str,
        include_memories: bool,
        now: datetime | None = None,
    ) -> PreparedChatContext:
        current_now = now or datetime.now(timezone.utc)
        timezone_name = await self.memory_service.get_timezone_name(session, user_id)
        current_datetime_text = format_current_datetime_context(current_now, timezone_name)
        memory_enabled = await self.memory_service.is_enabled(session, user_id)
        channel_aliases = (
            await self.channel_alias_service.list_aliases(session, guild_id=guild_id)
            if guild_id is not None
            else []
        )

        memory_retrieval_started_at = time.perf_counter()
        if include_memories and memory_enabled:
            memories = await self.memory_service.retrieve_relevant(
                session,
                user_id=user_id,
                guild_id=guild_id,
                query=prompt,
            )
        else:
            memories = []

        return PreparedChatContext(
            current_datetime_text=current_datetime_text,
            memories_block=format_memories_block(memories),
            channel_alias_block=format_channel_alias_block(channel_aliases),
            memory_enabled=memory_enabled,
            retrieved_memories=list(memories),
            memory_retrieval_ms=_elapsed_ms(memory_retrieval_started_at) if include_memories and memory_enabled else 0,
        )


def build_user_prompt(
    *,
    user_name: str,
    user_id: int,
    user_global_name: str,
    current_datetime_text: str,
    prompt: str,
    context_block: str,
    memories_block: str,
    channel_alias_block: str,
    search_requested: bool = False,
) -> str:
    prompt_text = (
        f"Current user: {user_name} (id: {user_id}, global: {user_global_name})\n\n"
        f"Current local date/time:\n{current_datetime_text}\n\n"
        f"Current request:\n{prompt}\n\n"
        f"Recent channel context:\n{context_block}\n\n"
        f"Relevant long-term memories:\n{memories_block}\n\n"
        f"Known channel aliases:\n{channel_alias_block}\n\n"
    )
    prompt_text += (
        "Available tools:\n"
        "- `web_search(query)`: use for fresh public web information when it would improve the answer. Prefer one comprehensive search first. Only search again if the first results are clearly insufficient or conflicting.\n"
        "- `extract_url_content(url, query?)`: use when the user gives one exact URL or asks about a specific page. Prefer this over web search when the target page is already known.\n"
        "- `create_reminder(message, remind_at)`: use when the user asks to be reminded later. `remind_at` should be an ISO 8601 local date/time when possible. Date-only values are allowed and default to 09:00 local time.\n"
        "- `send_channel_message(channel, message)`: send a message into another Discord channel in this server. Use a known channel alias or numeric channel ID. Only use this when the user explicitly wants a message posted somewhere else.\n"
        "\n"
    )
    prompt_text += (
        "If the current request includes image attachments, or the bot included replied-to or linked Discord messages and their images, use them as part of the current request.\n\n"
    )
    if search_requested:
        prompt_text += (
            "Required tool use for this request:\n"
            "- The user included `use search`, so you must call `web_search` at least once.\n\n"
        )
    prompt_text += (
        "Use tools when they materially help. Prefer one strong search query before trying multiple searches. You may call tools multiple times only if earlier results are insufficient. "
        "After tool results arrive, continue reasoning from those results and then answer.\n\n"
    )
    prompt_text += "Reply to the current request, not every message in the context window."
    return prompt_text


def format_memories_block(memories: Iterable[object]) -> str:
    rendered = [f"- [{memory.category}] {memory.summary}" for memory in memories]
    return "\n".join(rendered) if rendered else "(none)"


def format_channel_alias_block(aliases: Iterable[object]) -> str:
    rendered = [f"- {alias.alias}: channel_id={alias.channel_id}" for alias in aliases]
    return "\n".join(rendered) if rendered else "(none configured)"


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
