from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from nycti.formatting import format_current_date_context, format_current_datetime_context
from nycti.timing import elapsed_ms

MAX_RELATED_MEMORIES_PER_USER = 2
MAX_RELATED_MEMORY_USERS = 3
CHANNEL_SEND_HINT_RE = re.compile(
    r"\b(?:send|post|announce)\b.{0,80}\b(?:channel|chan|#|in|to)\b",
    re.IGNORECASE | re.DOTALL,
)
DATETIME_RELEVANCE_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|tonight|current|currently|latest|recent|now|"
    r"this\s+(?:week|month|year)|next\s+(?:week|month|year)|date|time|schedule|"
    r"remind|news|market|stock|price|earnings|weather)\b",
    re.IGNORECASE,
)
MEMORY_RELEVANCE_RE = re.compile(
    r"\b(?:i|i'm|im|me|my|mine|we|our|us|remember|again|prefer|favorite|recommend|"
    r"should\s+i|job|work|project|plan|goal|hobby|like|dislike)\b",
    re.IGNORECASE,
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
        mentioned_user_ids: Iterable[int] = (),
        now: datetime | None = None,
    ) -> PreparedChatContext:
        current_now = now or datetime.now(timezone.utc)
        timezone_name = await self.memory_service.get_timezone_name(session, user_id)
        if should_include_datetime_for_prompt(prompt):
            current_datetime_text = format_current_datetime_context(current_now, timezone_name)
        else:
            current_datetime_text = format_current_date_context(current_now, timezone_name)
        memory_enabled = await self.memory_service.is_enabled(session, user_id)
        memory_relevant = (
            include_memories
            and memory_enabled
            and should_retrieve_memories_for_prompt(prompt=prompt, context_text=context_text)
        )
        personal_profile = (
            await self.memory_service.get_personal_profile_md(session, user_id)
            if memory_relevant
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
        related_user_ids = select_related_memory_user_ids(
            current_user_id=user_id,
            mentioned_user_ids=mentioned_user_ids,
            member_aliases=member_aliases,
        )
        shared_embedding = None
        if (memory_relevant or related_user_ids) and hasattr(
            self.memory_service,
            "build_retrieval_query_embedding",
        ):
            shared_embedding = await self.memory_service.build_retrieval_query_embedding(
                session,
                query=prompt,
                guild_id=guild_id,
                usage_user_id=user_id,
            )
        if memory_relevant:
            memories = await self.memory_service.retrieve_relevant(
                session,
                user_id=user_id,
                guild_id=guild_id,
                query=prompt,
                query_embedding=shared_embedding,
                generate_embedding=False,
            )
        else:
            memories = []
        if include_memories and related_user_ids:
            related_memories = await self.memory_service.retrieve_relevant_for_users(
                session,
                user_ids=related_user_ids,
                guild_id=guild_id,
                query=build_related_memory_query(prompt=prompt, member_aliases=member_aliases),
                usage_user_id=user_id,
                query_embedding=shared_embedding,
                generate_embedding=False,
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
            memory_retrieval_ms=(
                elapsed_ms(memory_retrieval_started_at)
                if memory_relevant or related_user_ids
                else 0
            ),
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
) -> str:
    sections = [_format_current_user(user_name, user_id, user_global_name)]
    _append_optional_prompt_section(sections, "Owner/admin context", owner_context)
    _append_optional_prompt_section(sections, "Current local date/time", current_datetime_text)
    sections.append(f"Current request:\n{prompt}")
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
    if _has_prompt_content(context_block) or _has_prompt_content(extended_context_block):
        prompt_text += (
            "When summarizing chat or channel history, synthesize main topics, decisions, open questions, and notable links. Do not paste transcripts or exhaustive message lists unless asked for raw logs.\n\n"
        )
    if _has_prompt_content(extended_context_block):
        prompt_text += "Treat returned older context as lower-priority background.\n\n"
    if _has_prompt_content(personal_profile_block):
        prompt_text += (
            "Treat the short personal profile as optional background that may be stale, incomplete, or irrelevant. Do not overfit to it when the current request says otherwise.\n\n"
        )
    prompt_text += "Reply to the current request, not every message in the context window."
    return prompt_text


def _format_current_user(user_name: str, user_id: int, user_global_name: str) -> str:
    global_suffix = (
        f"; global={user_global_name}"
        if user_global_name.strip().casefold() != user_name.strip().casefold()
        else ""
    )
    return f"Current user: {user_name} (id={user_id}{global_suffix})"


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
        "No owner/admin user ID is configured.",
    }:
        return False
    if "current user is not the owner/admin" in cleaned.casefold():
        return False
    if "current user is not the configured bot owner/admin" in cleaned.casefold():
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
    mentioned_user_ids: Iterable[int],
    member_aliases: Iterable[object],
) -> list[int]:
    user_ids = [int(user_id) for user_id in mentioned_user_ids]
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


def should_include_datetime_for_prompt(prompt: str) -> bool:
    return bool(DATETIME_RELEVANCE_RE.search(prompt))


def should_retrieve_memories_for_prompt(*, prompt: str, context_text: str) -> bool:
    combined = f"{prompt}\n{context_text}".strip()
    if not combined:
        return False
    return bool(MEMORY_RELEVANCE_RE.search(combined))
