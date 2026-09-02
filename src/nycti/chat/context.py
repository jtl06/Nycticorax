from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from nycti.formatting import format_current_date_context, format_current_datetime_context
from nycti.member_aliases import format_member_reference_block, member_identity_names
from nycti.memory.lifecycle import build_memory_retrieval_plan
from nycti.procedures.service import format_procedure_matches
from nycti.timing import elapsed_ms

MAX_RELATED_MEMORIES_PER_USER = 8
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
FINANCE_MEMORY_RELEVANCE_RE = re.compile(
    r"\b(?:stock|stocks|ticker|tickers|watchlist|portfolio|shares|earnings)\b|"
    r"\$[A-Za-z][A-Za-z0-9.-]{0,9}\b",
    re.IGNORECASE,
)
TICKER_FORM_RE = re.compile(r"(?<![A-Z0-9])[A-Z][A-Z0-9.=-]{1,9}(?![A-Z0-9])")
PUBLIC_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
WATCHLIST_REPORT_RE = re.compile(
    r"\b(?:my|our)\s+(?:stocks?|watchlist|portfolio)\b|"
    r"\bstocks?\s+(?:i|we)\s+(?:care(?:\s+about)?|follow|track)\b|"
    r"\b(?:watchlist|portfolio)\s+(?:update|report|today|now)\b|"
    r"\b(?:market|stock|overnight|premarket|after[- ]hours?|24[- ]hour)\s+report\b|"
    r"^\s*(?:stocks?|watchlist|portfolio|overnight\s+report)\s*[?!.]*\s*$",
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
    memory_snapshot_block: str
    memory_snapshot_source_count: int
    market_watchlist_block: str
    market_watchlist_symbols: tuple[str, ...]
    memory_enabled: bool
    retrieved_memories: list[object]
    memory_retrieval_ms: int
    procedure_memory_block: str = ""
    procedure_memory_ids: tuple[int, ...] = ()


class ChatContextBuilder:
    def __init__(
        self,
        *,
        memory_service: Any,
        channel_alias_service: Any,
        member_alias_service: Any,
        procedure_memory_service: Any | None = None,
    ) -> None:
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.member_alias_service = member_alias_service
        self.procedure_memory_service = procedure_memory_service

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
        timing_metrics: dict[str, int] | None = None,
    ) -> PreparedChatContext:
        prepare_started_at = time.perf_counter()
        current_now = now or datetime.now(timezone.utc)
        mentioned_ids = tuple(mentioned_user_ids)
        personal_memory_relevant = should_retrieve_personal_memories_for_prompt(
            prompt=prompt,
            context_text=context_text,
        )

        prompt_settings_lookup = getattr(
            self.memory_service,
            "get_prompt_settings",
            None,
        )
        prompt_settings = None
        if callable(prompt_settings_lookup):
            stage_started_at = time.perf_counter()
            prompt_settings = await prompt_settings_lookup(session, user_id)
            _record_context_timing(timing_metrics, "ctx_settings_ms", stage_started_at)
            timezone_name = str(prompt_settings.timezone_name)
            memory_enabled = bool(prompt_settings.memory_enabled)
            if timing_metrics is not None:
                timing_metrics["ctx_tz_ms"] = 0
                timing_metrics["ctx_mem_flag_ms"] = 0
        else:
            stage_started_at = time.perf_counter()
            timezone_name = await self.memory_service.get_timezone_name(session, user_id)
            _record_context_timing(timing_metrics, "ctx_tz_ms", stage_started_at)

            stage_started_at = time.perf_counter()
            memory_enabled = await self.memory_service.is_enabled(session, user_id)
            _record_context_timing(timing_metrics, "ctx_mem_flag_ms", stage_started_at)
        if should_include_datetime_for_prompt(prompt):
            current_datetime_text = format_current_datetime_context(current_now, timezone_name)
        else:
            current_datetime_text = format_current_date_context(current_now, timezone_name)

        memory_relevant = include_memories and memory_enabled
        embedding_generator = getattr(
            self.memory_service,
            "generate_retrieval_query_embedding",
            None,
        )
        embedding_task: asyncio.Task[tuple[object | None, int]] | None = None
        if callable(embedding_generator) and include_memories and (
            (memory_enabled and personal_memory_relevant) or mentioned_ids
        ):

            async def generate_embedding() -> tuple[object | None, int]:
                embedding_started_at = time.perf_counter()
                result = await embedding_generator(query=prompt)
                return result, elapsed_ms(embedding_started_at)

            embedding_task = asyncio.create_task(generate_embedding())
        if memory_relevant:
            if prompt_settings is not None:
                personal_profile = str(prompt_settings.personal_profile_md)
                if timing_metrics is not None:
                    timing_metrics["ctx_profile_ms"] = 0
            else:
                stage_started_at = time.perf_counter()
                personal_profile = await self.memory_service.get_personal_profile_md(
                    session,
                    user_id,
                )
                _record_context_timing(timing_metrics, "ctx_profile_ms", stage_started_at)

            stage_started_at = time.perf_counter()
            snapshot_kwargs: dict[str, object] = {
                "user_id": user_id,
                "guild_id": guild_id,
            }
            if prompt_settings is not None:
                snapshot_kwargs["memory_enabled"] = memory_enabled
            memory_snapshots = await self.memory_service.get_memory_snapshot_blocks(
                session,
                **snapshot_kwargs,
            )
            _record_context_timing(timing_metrics, "ctx_snapshot_ms", stage_started_at)
        else:
            personal_profile = ""
            memory_snapshots = None
        watchlist_lookup = getattr(
            self.memory_service,
            "get_active_market_watchlist",
            None,
        )
        if memory_relevant and callable(watchlist_lookup):
            stage_started_at = time.perf_counter()
            active_watchlist = await watchlist_lookup(
                session,
                user_id=user_id,
                guild_id=guild_id,
                now=current_now,
                memory_enabled=memory_enabled,
            )
            _record_context_timing(timing_metrics, "ctx_watchlist_ms", stage_started_at)
        else:
            active_watchlist = None
        should_include_channel_aliases = guild_id is not None and should_include_channel_aliases_for_prompt(
            prompt=prompt,
            context_text=context_text,
        )
        if should_include_channel_aliases:
            stage_started_at = time.perf_counter()
            channel_aliases = await self.channel_alias_service.list_aliases(
                session,
                guild_id=guild_id,
            )
            _record_context_timing(timing_metrics, "ctx_chan_alias_ms", stage_started_at)
        else:
            channel_aliases = []
        if guild_id is not None:
            reference_text = f"{prompt}\n{context_text}"
            reference_lookup = getattr(
                self.member_alias_service,
                "list_matching_references",
                None,
            )
            if callable(reference_lookup):
                stage_started_at = time.perf_counter()
                member_aliases, member_identities = await reference_lookup(
                    session,
                    guild_id=guild_id,
                    text=reference_text,
                )
                _record_context_timing(
                    timing_metrics,
                    "ctx_member_refs_ms",
                    stage_started_at,
                )
                if timing_metrics is not None:
                    timing_metrics["ctx_member_alias_ms"] = 0
                    timing_metrics["ctx_member_ids_ms"] = 0
            else:
                stage_started_at = time.perf_counter()
                member_aliases = await self.member_alias_service.list_matching_aliases(
                    session,
                    guild_id=guild_id,
                    text=reference_text,
                )
                _record_context_timing(
                    timing_metrics,
                    "ctx_member_alias_ms",
                    stage_started_at,
                )

                stage_started_at = time.perf_counter()
                member_identities = await self.member_alias_service.list_matching_identities(
                    session,
                    guild_id=guild_id,
                    text=reference_text,
                )
                _record_context_timing(
                    timing_metrics,
                    "ctx_member_ids_ms",
                    stage_started_at,
                )
        else:
            member_aliases = []
            member_identities = []

        if guild_id is not None and self.procedure_memory_service is not None:
            stage_started_at = time.perf_counter()
            procedure_matches = await self.procedure_memory_service.retrieve(
                session,
                guild_id=guild_id,
                query=prompt,
                limit=1,
            )
            _record_context_timing(timing_metrics, "ctx_procedure_ms", stage_started_at)
        else:
            procedure_matches = []

        memory_retrieval_started_at = time.perf_counter()
        related_user_ids = select_related_memory_user_ids(
            current_user_id=user_id,
            mentioned_user_ids=mentioned_ids,
            member_aliases=member_aliases,
            member_identities=member_identities,
        )
        enabled_user_lookup = getattr(
            self.memory_service,
            "get_enabled_user_ids",
            None,
        )
        if include_memories and related_user_ids and callable(enabled_user_lookup):
            stage_started_at = time.perf_counter()
            related_user_ids = list(
                await enabled_user_lookup(
                    session,
                    user_ids=related_user_ids,
                )
            )
            _record_context_timing(timing_metrics, "ctx_related_auth_ms", stage_started_at)
        related_memory_relevant = include_memories and bool(related_user_ids)
        shared_embedding = None
        semantic_prefetch_relevant = (
            include_memories and memory_enabled and personal_memory_relevant
        )
        if (semantic_prefetch_relevant or related_memory_relevant) and hasattr(
            self.memory_service,
            "build_retrieval_query_embedding",
        ):
            if embedding_task is not None:
                wait_started_at = time.perf_counter()
                embedding_result, embedding_elapsed_ms = await embedding_task
                if timing_metrics is not None:
                    timing_metrics["ctx_embed_ms"] = embedding_elapsed_ms
                    timing_metrics["ctx_embed_wait_ms"] = elapsed_ms(wait_started_at)
                if embedding_result is not None:
                    usage_recorder = getattr(
                        self.memory_service,
                        "record_retrieval_query_embedding_usage",
                        None,
                    )
                    if callable(usage_recorder):
                        stage_started_at = time.perf_counter()
                        await usage_recorder(
                            session,
                            guild_id=guild_id,
                            usage_user_id=user_id,
                            embedding_result=embedding_result,
                        )
                        _record_context_timing(
                            timing_metrics,
                            "ctx_embed_usage_ms",
                            stage_started_at,
                        )
                    shared_embedding = getattr(
                        embedding_result,
                        "embedding",
                        None,
                    )
            else:
                stage_started_at = time.perf_counter()
                shared_embedding = await self.memory_service.build_retrieval_query_embedding(
                    session,
                    query=prompt,
                    guild_id=guild_id,
                    usage_user_id=user_id,
                )
                _record_context_timing(timing_metrics, "ctx_embed_ms", stage_started_at)
        elif embedding_task is not None:
            embedding_task.cancel()
            await asyncio.gather(embedding_task, return_exceptions=True)
        if memory_relevant:
            stage_started_at = time.perf_counter()
            retriever = getattr(self.memory_service, "retriever", None)
            retriever_settings = getattr(retriever, "settings", None)
            retrieval_plan = build_memory_retrieval_plan(
                f"{prompt}\n{context_text}",
                maximum=getattr(
                    retriever_settings,
                    "memory_retrieval_limit",
                    6,
                ),
            )
            memories = await self.memory_service.retrieve_relevant(
                session,
                user_id=user_id,
                requester_user_id=user_id,
                guild_id=guild_id,
                query=prompt,
                query_embedding=shared_embedding,
                generate_embedding=False,
                memory_enabled=memory_enabled,
                limit=retrieval_plan.limit,
                include_history=retrieval_plan.include_history,
            )
            _record_context_timing(timing_metrics, "ctx_mem_query_ms", stage_started_at)
        else:
            memories = []
        if related_memory_relevant:
            stage_started_at = time.perf_counter()
            related_plan = build_memory_retrieval_plan(
                f"{prompt}\n{context_text}",
                maximum=MAX_RELATED_MEMORIES_PER_USER,
            )
            related_memories = await self.memory_service.retrieve_relevant_for_users(
                session,
                user_ids=related_user_ids,
                requester_user_id=user_id,
                guild_id=guild_id,
                query=build_related_memory_query(
                    prompt=prompt,
                    member_aliases=member_aliases,
                    member_identities=member_identities,
                ),
                usage_user_id=user_id,
                query_embedding=shared_embedding,
                generate_embedding=False,
                limit=related_plan.limit,
                include_history=related_plan.include_history,
            )
            _record_context_timing(timing_metrics, "ctx_related_mem_ms", stage_started_at)
        else:
            related_memories = {}

        stage_started_at = time.perf_counter()
        prepared = PreparedChatContext(
            current_datetime_text=current_datetime_text,
            memories_block=format_memories_block(memories),
            personal_profile_block=format_personal_profile_block(personal_profile),
            channel_alias_block=format_channel_alias_block(channel_aliases),
            member_alias_block=format_member_reference_block(
                member_aliases,
                member_identities,
            ),
            mentioned_user_memories_block=format_related_memories_block(related_memories),
            memory_snapshot_block=(
                memory_snapshots.rendered if memory_snapshots is not None else ""
            ),
            memory_snapshot_source_count=(
                memory_snapshots.source_count if memory_snapshots is not None else 0
            ),
            market_watchlist_block=format_market_watchlist_block(active_watchlist),
            market_watchlist_symbols=tuple(
                getattr(active_watchlist, "symbols", ()) or ()
            ),
            memory_enabled=memory_enabled,
            retrieved_memories=list(memories),
            memory_retrieval_ms=(
                elapsed_ms(memory_retrieval_started_at)
                if memory_relevant or related_user_ids
                else 0
            ),
            procedure_memory_block=format_procedure_matches(procedure_matches),
            procedure_memory_ids=tuple(
                match.procedure_id for match in procedure_matches
            ),
        )
        _record_context_timing(timing_metrics, "ctx_prepare_format_ms", stage_started_at)
        _record_context_timing(timing_metrics, "ctx_prepare_ms", prepare_started_at)
        return prepared


def _record_context_timing(
    timing_metrics: dict[str, int] | None,
    key: str,
    started_at: float,
) -> None:
    if timing_metrics is not None:
        timing_metrics[key] = elapsed_ms(started_at)


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
    memory_snapshot_block: str = "",
    market_watchlist_block: str = "",
    procedure_memory_block: str = "",
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
    _append_optional_prompt_section(sections, "Core memory snapshot", memory_snapshot_block)
    _append_optional_prompt_section(
        sections,
        "Relevant learned tool procedure",
        procedure_memory_block,
    )
    _append_optional_prompt_section(
        sections,
        "Active market watchlist",
        market_watchlist_block,
    )
    _append_optional_prompt_section(sections, "Relevant long-term memories", memories_block)
    _append_optional_prompt_section(sections, "Known channel aliases", channel_alias_block)
    _append_optional_prompt_section(sections, "Relevant Discord members and aliases", member_alias_block)
    _append_optional_prompt_section(sections, "Relevant memories for mentioned users", mentioned_user_memories_block)

    prompt_text = "\n\n".join(sections) + "\n\n"
    if _has_prompt_content(image_context_block) or _has_prompt_content(vision_context_block):
        prompt_text += (
            "If the current request includes image attachments, or the bot included recent-context, replied-to, or linked Discord messages and their images, use them as part of the current request. Use the included image context block to match each image to its source message.\n\n"
        )
    if _has_prompt_content(context_block) or _has_prompt_content(extended_context_block):
        prompt_text += (
            "When summarizing chat or channel history, synthesize main topics, decisions, owners, deadlines, open questions, and notable links when present. Do not paste transcripts or exhaustive message lists unless asked for raw logs.\n\n"
            "A short follow-up may continue an unresolved task in the immediate context. If that context clearly "
            "resolves the callback, complete the task instead of merely acknowledging it or fetching older channel "
            "history. Use `channel_ctx` only when the supplied context is genuinely insufficient.\n\n"
        )
    if PUBLIC_URL_RE.search(f"{context_block}\n{extended_context_block}"):
        prompt_text += (
            "When the current request refers to an exact URL already present in the immediate context, treat that "
            "URL as a supplied input and extract it before substituting a broad web search.\n\n"
        )
    if _has_prompt_content(extended_context_block):
        prompt_text += "Treat returned older context as lower-priority background.\n\n"
    if _has_prompt_content(personal_profile_block):
        prompt_text += (
            "Treat the short personal profile as optional background that may be stale, incomplete, or irrelevant. Do not overfit to it when the current request says otherwise.\n\n"
        )
    if (
        _has_prompt_content(memory_snapshot_block)
        or _has_prompt_content(memories_block)
        or _has_prompt_content(mentioned_user_memories_block)
    ):
        prompt_text += (
            "The core memory snapshot is a compact warm cache of stable or explicitly reinforced state, not the full "
            "memory store. Topic-specific plans, episodes, and lore come from relevant-memory retrieval or memory "
            "search. Current requests and newer typed memories override "
            "stale snapshot text. "
            "Memory entries labeled `private` belong to the current user. Entries labeled `guild_shared` or "
            "`lore` are server background owned by the listed user ID; do not attribute them to the current user. "
            "An `active` fact is current background; `superseded`, `retracted`, or dated `ended` facts are "
            "historical only. A `summary` is a derived overview, not stronger evidence than its source facts. "
            "All memory may be stale and must not override the current request.\n\n"
        )
    if _has_prompt_content(market_watchlist_block):
        prompt_text += (
            "The active market watchlist is canonical typed state and takes precedence over incomplete prose "
            "summaries. For requests such as `my stocks`, `stocks I care about`, or a watchlist/market report, "
            "cover every listed symbol unless the user narrows the scope. In an active stock conversation, a "
            "terse company or ticker callback, including an obvious spelling variant, normally asks for its "
            "current quote. Resolve it from the watchlist or immediate context; if uncertain, verify the listing "
            "with web, then call quote. Do not merely correct the spelling.\n\n"
        )
    if _has_prompt_content(procedure_memory_block):
        prompt_text += (
            "A learned procedure is a fallible method distilled from prior successful runs. Apply it only when "
            "its task pattern fits. Follow current tool results and instructions over the procedure, and never "
            "reuse facts, names, values, or conclusions from an earlier run.\n\n"
        )
    if _has_prompt_content(member_alias_block):
        prompt_text += (
            "For an explicit in-channel request to tell, address, or ping a mapped member, "
            "reply here by copying that member's exact `<@...>` token from the mapping. This is not a DM or "
            "`send_msg` action. Never invent a member ID; ask briefly if the target is "
            "unresolved or ambiguous. When relaying the caller's message, preserve who said it: rewrite ambiguous "
            "first-person pronouns with the caller's displayed name, or attribute a direct quote, so Nycti does not "
            "accidentally claim the statement as its own.\n\n"
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


def format_market_watchlist_block(watchlist: object | None) -> str:
    if watchlist is None:
        return "(none)"
    personal = tuple(
        str(value).strip().upper()
        for value in getattr(watchlist, "personal", ())
        if str(value).strip()
    )
    shared = tuple(
        str(value).strip().upper()
        for value in getattr(watchlist, "shared", ())
        if str(value).strip()
    )
    lines: list[str] = []
    if personal:
        lines.append("Personal: " + ", ".join(dict.fromkeys(personal)))
    if shared:
        lines.append("Shared market-report defaults: " + ", ".join(dict.fromkeys(shared)))
    return "\n".join(lines) or "(none)"


def required_watchlist_symbols_for_request(
    request_text: str,
    symbols: Iterable[str],
) -> tuple[str, ...]:
    if WATCHLIST_REPORT_RE.search(request_text) is None:
        return ()
    normalized = [
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ]
    return tuple(dict.fromkeys(normalized))


def format_memories_block(memories: Iterable[object]) -> str:
    rendered = []
    for memory in memories:
        visibility = str(getattr(memory, "visibility", "private"))
        memory_kind = str(getattr(memory, "memory_kind", "fact") or "fact")
        status = str(getattr(memory, "status", "active") or "active")
        predicate = str(getattr(memory, "predicate", "") or "").strip()
        entities = list(getattr(memory, "related_entities", None) or [])[:3]
        metadata = [memory_kind]
        if status != "active":
            metadata.append(f"status={status}")
            valid_until = getattr(memory, "valid_until", None)
            if valid_until is not None:
                metadata.append(f"ended={valid_until.date().isoformat()}")
        if predicate:
            metadata.append(f"key={predicate}")
        if entities:
            metadata.append("entities=" + ",".join(entities))
        if visibility == "private":
            rendered.append(
                f"- [private; {'; '.join(metadata)}; {memory.category}] {memory.summary}"
            )
        else:
            rendered.append(
                f"- [{visibility}; owner_user_id={memory.user_id}; "
                f"{'; '.join(metadata)}; {memory.category}] {memory.summary}"
            )
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
    return format_member_reference_block(list(aliases), [])


def format_related_memories_block(related_memories: dict[int, list[object]]) -> str:
    lines: list[str] = []
    for target_user_id, memories in related_memories.items():
        for memory in memories[:MAX_RELATED_MEMORIES_PER_USER]:
            kind = str(getattr(memory, "memory_kind", "fact") or "fact")
            status = str(getattr(memory, "status", "active") or "active")
            predicate = str(getattr(memory, "predicate", "") or "").strip()
            metadata = [kind]
            if status != "active":
                metadata.append(f"status={status}")
            if predicate:
                metadata.append(f"key={predicate}")
            lines.append(
                f"- user_id={target_user_id} [{'; '.join(metadata)}; {memory.category}] "
                f"{memory.summary}"
            )
    return "\n".join(lines) if lines else "(none)"


def select_related_memory_user_ids(
    *,
    current_user_id: int,
    mentioned_user_ids: Iterable[int],
    member_aliases: Iterable[object],
    member_identities: Iterable[object] = (),
) -> list[int]:
    user_ids = [int(user_id) for user_id in mentioned_user_ids]
    user_ids.extend(int(alias.user_id) for alias in member_aliases)
    user_ids.extend(int(identity.user_id) for identity in member_identities)
    return [
        target_user_id
        for target_user_id in dict.fromkeys(user_ids)
        if target_user_id != current_user_id
    ][:MAX_RELATED_MEMORY_USERS]


def build_related_memory_query(
    *,
    prompt: str,
    member_aliases: Iterable[object],
    member_identities: Iterable[object] = (),
) -> str:
    alias_parts = [
        f"{alias.alias}=user_id={alias.user_id}"
        for alias in member_aliases
    ]
    alias_parts.extend(
        f"{names[0]}=user_id={identity.user_id}"
        for identity in member_identities
        if (names := member_identity_names(identity))
    )
    if not alias_parts:
        return prompt
    return f"{prompt}\nMatched aliases: " + ", ".join(alias_parts)


def should_include_channel_aliases_for_prompt(*, prompt: str, context_text: str) -> bool:
    combined = f"{prompt}\n{context_text}"
    return bool(CHANNEL_SEND_HINT_RE.search(combined))


def should_include_datetime_for_prompt(prompt: str) -> bool:
    return bool(DATETIME_RELEVANCE_RE.search(prompt))


def should_retrieve_memories_for_prompt(*, prompt: str, context_text: str) -> bool:
    return should_retrieve_personal_memories_for_prompt(
        prompt=prompt,
        context_text=context_text,
    ) or should_retrieve_ticker_memories_for_prompt(
        prompt=prompt,
        context_text=context_text,
    )


def should_retrieve_personal_memories_for_prompt(*, prompt: str, context_text: str) -> bool:
    combined = f"{prompt}\n{context_text}".strip()
    if not combined:
        return False
    return bool(MEMORY_RELEVANCE_RE.search(combined))


def should_retrieve_ticker_memories_for_prompt(*, prompt: str, context_text: str) -> bool:
    combined = f"{prompt}\n{context_text}".strip()
    if not combined:
        return False
    return bool(
        FINANCE_MEMORY_RELEVANCE_RE.search(combined)
        or TICKER_FORM_RE.search(combined)
    )
