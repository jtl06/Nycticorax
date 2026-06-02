from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from nycti.chat.tools.schemas import GET_CHANNEL_CONTEXT_TOOL_NAME, WEB_SEARCH_TOOL_NAME
from nycti.formatting import format_current_datetime_context

MAX_RELATED_MEMORY_USERS = 3
MAX_RELATED_MEMORIES_PER_USER = 2
USER_ID_RE = re.compile(r"\buser_id=(\d+)\b")
CHANNEL_SEND_HINT_RE = re.compile(
    r"\b(?:send|post|say|tell|announce|drop|write)\b.{0,80}\b(?:channel|chan|#|in|to)\b",
    re.IGNORECASE | re.DOTALL,
)

@dataclass(slots=True)
class PreparedChatContext:
    current_datetime_text: str
    memories_block: str
    personal_profile_block: str
    channel_alias_block: str
    member_alias_block: str
    mentioned_user_memories_block: str
    memory_enabled: bool
    retrieved_memories: list[object]
    memory_retrieval_ms: int


class ChatContextBuilder:
    def __init__(
        self,
        *,
        memory_service: Any,
        channel_alias_service: Any,
        member_alias_service: Any,
    ) -> None:
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.member_alias_service = member_alias_service

    async def prepare(
        self,
        session,
        *,
        guild_id: int | None,
        user_id: int,
        prompt: str,
        context_text: str,
        include_memories: bool,
        now: datetime | None = None,
    ) -> PreparedChatContext:
        current_now = now or datetime.now(timezone.utc)
        timezone_name = await self.memory_service.get_timezone_name(session, user_id)
        current_datetime_text = format_current_datetime_context(current_now, timezone_name)
        memory_enabled = await self.memory_service.is_enabled(session, user_id)
        personal_profile = (
            await self.memory_service.get_personal_profile_md(session, user_id)
            if memory_enabled
            else ""
        )
        should_include_channel_aliases = guild_id is not None and should_include_channel_aliases_for_prompt(
            prompt=prompt,
            context_text=context_text,
        )
        channel_aliases = (
            await self.channel_alias_service.list_aliases(session, guild_id=guild_id)
            if should_include_channel_aliases
            else []
        )
        member_aliases = (
            await self.member_alias_service.list_matching_aliases(
                session,
                guild_id=guild_id,
                text=f"{prompt}\n{context_text}",
            )
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
        related_user_ids = select_related_memory_user_ids(
            current_user_id=user_id,
            prompt=prompt,
            context_text=context_text,
            member_aliases=member_aliases,
        )
        if include_memories and related_user_ids:
            related_memories = await self.memory_service.retrieve_relevant_for_users(
                session,
                user_ids=related_user_ids,
                guild_id=guild_id,
                query=build_related_memory_query(prompt=prompt, member_aliases=member_aliases),
                usage_user_id=user_id,
            )
        else:
            related_memories = {}

        return PreparedChatContext(
            current_datetime_text=current_datetime_text,
            memories_block=format_memories_block(memories),
            personal_profile_block=format_personal_profile_block(personal_profile),
            channel_alias_block=format_channel_alias_block(channel_aliases),
            member_alias_block=format_member_alias_block(member_aliases),
            mentioned_user_memories_block=format_related_memories_block(related_memories),
            memory_enabled=memory_enabled,
            retrieved_memories=list(memories),
            memory_retrieval_ms=_elapsed_ms(memory_retrieval_started_at) if include_memories and memory_enabled else 0,
        )


def build_user_prompt(
    *,
    user_name: str,
    user_id: int,
    user_global_name: str,
    owner_context: str,
    current_datetime_text: str,
    prompt: str,
    context_block: str,
    extended_context_block: str,
    image_context_block: str,
    vision_context_block: str,
    personal_profile_block: str,
    memories_block: str,
    channel_alias_block: str,
    member_alias_block: str,
    mentioned_user_memories_block: str,
    search_requested: bool = False,
) -> str:
    sections = [
        f"Current user: {user_name} (id: {user_id}, global: {user_global_name})",
        f"Owner/admin context:\n{owner_context}",
        f"Current local date/time:\n{current_datetime_text}",
        f"Current request:\n{prompt}",
    ]
    _append_optional_prompt_section(sections, "Recent channel context", context_block)
    _append_optional_prompt_section(sections, "Extended channel context", extended_context_block)
    _append_optional_prompt_section(sections, "Included image context", image_context_block)
    _append_optional_prompt_section(sections, "Image analysis", vision_context_block)
    _append_optional_prompt_section(sections, "Calling user's short personal profile", personal_profile_block)
    _append_optional_prompt_section(sections, "Relevant long-term memories", memories_block)
    _append_optional_prompt_section(sections, "Known channel aliases", channel_alias_block)
    _append_optional_prompt_section(sections, "Relevant member nicknames/aliases", member_alias_block)
    _append_optional_prompt_section(sections, "Relevant memories for mentioned users", mentioned_user_memories_block)

    prompt_text = "\n\n".join(sections) + "\n\n"
    if _has_prompt_content(image_context_block) or _has_prompt_content(vision_context_block):
        prompt_text += (
            "If the current request includes image attachments, or the bot included recent-context, replied-to, or linked Discord messages and their images, use them as part of the current request. Use the included image context block to match each image to its source message.\n\n"
        )
    prompt_text += (
        "The provided local date/time is authoritative. Use it for the current year and relative dates like today, tomorrow, yesterday, this week, and next week.\n\n"
    )
    prompt_text += (
        f"For older Discord context, use `{GET_CHANNEL_CONTEXT_TOOL_NAME}` instead of guessing. Treat returned older context as lower-priority background.\n\n"
    )
    if _has_prompt_content(context_block) or _has_prompt_content(extended_context_block):
        prompt_text += (
            "When summarizing chat or channel history, synthesize main topics, decisions, open questions, and notable links. Do not paste transcripts or exhaustive message lists unless asked for raw logs.\n\n"
        )
    if _has_prompt_content(personal_profile_block):
        prompt_text += (
            "Treat the short personal profile as optional background that may be stale, incomplete, or irrelevant. Do not overfit to it when the current request says otherwise.\n\n"
        )
    if search_requested:
        prompt_text += (
            "Required tool use for this request:\n"
            f"- The user included `use search`, so you must call `{WEB_SEARCH_TOOL_NAME}` at least once.\n\n"
        )
    prompt_text += (
        "Use tools when they materially help. Prefer one strong search/query first, and call more only if results are insufficient. After tools return, reason from the results and answer.\n\n"
    )
    prompt_text += "Reply to the current request, not every message in the context window."
    return prompt_text


def _append_optional_prompt_section(sections: list[str], title: str, body: str) -> None:
    cleaned = body.strip()
    if not _has_prompt_content(cleaned):
        return
    sections.append(f"{title}:\n{cleaned}")


def _has_prompt_content(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    if cleaned in {
        "(none)",
        "(none configured)",
        "(none matched)",
        "(no recent context)",
        "(no included images)",
        "(no image analysis)",
        "(not requested yet; use `channel_ctx` if older Discord context is needed)",
    }:
        return False
    return True


def format_memories_block(memories: Iterable[object]) -> str:
    rendered = [f"- [{memory.category}] {memory.summary}" for memory in memories]
    return "\n".join(rendered) if rendered else "(none)"


def format_personal_profile_block(profile_md: str) -> str:
    cleaned = profile_md.strip()
    if not cleaned:
        return "(none)"
    return cleaned


def format_channel_alias_block(aliases: Iterable[object]) -> str:
    rendered = [f"- {alias.alias}: channel_id={alias.channel_id}" for alias in aliases]
    return "\n".join(rendered) if rendered else "(none configured)"


def format_member_alias_block(aliases: Iterable[object]) -> str:
    rendered = []
    for alias in aliases:
        note = f" ({alias.note})" if getattr(alias, "note", "") else ""
        rendered.append(f"- {alias.alias}: user_id={alias.user_id}{note}")
    return "\n".join(rendered) if rendered else "(none matched)"


def format_related_memories_block(related_memories: dict[int, list[object]]) -> str:
    lines: list[str] = []
    for target_user_id, memories in related_memories.items():
        for memory in memories[:MAX_RELATED_MEMORIES_PER_USER]:
            lines.append(f"- user_id={target_user_id} [{memory.category}] {memory.summary}")
    return "\n".join(lines) if lines else "(none)"


def select_related_memory_user_ids(
    *,
    current_user_id: int,
    prompt: str,
    context_text: str,
    member_aliases: Iterable[object],
) -> list[int]:
    combined_text = f"{prompt}\n{context_text}"
    user_ids = [int(match) for match in USER_ID_RE.findall(combined_text)]
    user_ids.extend(int(alias.user_id) for alias in member_aliases)
    return [
        target_user_id
        for target_user_id in dict.fromkeys(user_ids)
        if target_user_id != current_user_id
    ][:MAX_RELATED_MEMORY_USERS]


def build_related_memory_query(*, prompt: str, member_aliases: Iterable[object]) -> str:
    alias_parts = [
        f"{alias.alias}=user_id={alias.user_id}"
        for alias in member_aliases
    ]
    if not alias_parts:
        return prompt
    return f"{prompt}\nMatched aliases: " + ", ".join(alias_parts)


def should_include_channel_aliases_for_prompt(*, prompt: str, context_text: str) -> bool:
    combined = f"{prompt}\n{context_text}"
    return bool(CHANNEL_SEND_HINT_RE.search(combined))


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
