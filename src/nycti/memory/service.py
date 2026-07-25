from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import AppState, Memory, UserSettings
from nycti.formatting import parse_json_object_payload
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.memory.extractor import MemoryExtractor, coerce_json_bool
from nycti.memory.profile import clean_profile_markdown, strip_noncaller_profile_lines
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.filtering import contains_sensitive_pattern, contains_transient_memory_pattern
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    MemoryOperation,
    MemoryStatus,
    build_memory_retrieval_plan,
    build_subject_key,
    normalize_related_entities,
)
from nycti.memory.visibility import (
    MemoryVisibility,
    normalize_memory_visibility,
    validate_memory_visibility_context,
)
from nycti.timezones import DEFAULT_TIMEZONE_NAME, resolve_timezone_name
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
MEMORY_CONSOLIDATION_STATE_PREFIX = "memory_consolidated_at"


class MemoryService:
    def __init__(
        self,
        extractor: MemoryExtractor,
        retriever: MemoryRetriever,
        *,
        llm_client: OpenAIClient,
        embedding_model: str | None,
    ) -> None:
        self.extractor = extractor
        self.retriever = retriever
        self.llm_client = llm_client
        self.embedding_model = embedding_model

    async def is_enabled(self, session: AsyncSession, user_id: int) -> bool:
        settings = await self._get_or_create_settings(session, user_id)
        return settings.memory_enabled

    async def set_enabled(self, session: AsyncSession, user_id: int, enabled: bool) -> bool:
        settings = await self._get_or_create_settings(session, user_id)
        settings.memory_enabled = enabled
        await session.flush()
        return settings.memory_enabled

    async def get_timezone_name(self, session: AsyncSession, user_id: int) -> str:
        settings = await self._get_or_create_settings(session, user_id)
        return resolve_timezone_name(settings.timezone_name)

    async def set_timezone_name(self, session: AsyncSession, user_id: int, timezone_name: str) -> str:
        settings = await self._get_or_create_settings(session, user_id)
        settings.timezone_name = resolve_timezone_name(timezone_name)
        await session.flush()
        return settings.timezone_name

    async def get_personal_profile_md(self, session: AsyncSession, user_id: int) -> str:
        settings = await self._get_or_create_settings(session, user_id)
        return settings.personal_profile_md.strip()

    async def maybe_update_personal_profile(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> LLMResult | None:
        settings = await self._get_or_create_settings(session, user_id)
        if not settings.memory_enabled:
            return None
        availability_check = getattr(self.llm_client, "is_model_available", None)
        if callable(availability_check) and not availability_check(
            self.extractor.settings.openai_memory_model
        ):
            return None
        try:
            result = await self.llm_client.complete_chat(
                model=self.extractor.settings.openai_memory_model,
                feature="personal_profile_update",
                max_tokens=300,
                temperature=0,
                request_timeout_seconds=8.0,
                request_max_retries=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Maintain a very short markdown profile for one Discord user. "
                            "Keep only durable, useful, non-sensitive personal context for future replies. "
                            "Only the current message is authored by this user. Use recent context solely to resolve references; never copy another speaker's facts into this profile. "
                            "Do not store secrets, credentials, legal identifiers, financial account data, medical details, or one-off chatter. "
                            "Preserve existing durable facts unless the current user's message explicitly updates or contradicts them. "
                            "The profile must be at most 140 tokens. Return JSON only with keys: profile_md, should_update."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Existing profile:\n{settings.personal_profile_md.strip() or '(none)'}\n\n"
                            f"Current message:\n{current_message}\n\n"
                            f"Recent context:\n{recent_context or '(none)'}\n\n"
                            "Update the profile only if there is durable useful personal info. "
                            "Use short markdown bullets. If no update is useful, return the existing profile and should_update=false."
                        ),
                    },
                ],
            )
        except Exception as exc:  # defensive optional enrichment
            LOGGER.warning(
                "Personal profile enrichment deferred after provider failure: %s",
                " ".join(str(exc).split())[:240],
            )
            return None
        payload = parse_json_object_payload(result.text)
        if not payload:
            return result
        if not coerce_json_bool(payload.get("should_update")):
            return result
        profile_md = clean_profile_markdown(str(payload.get("profile_md", "")))
        profile_md = strip_noncaller_profile_lines(profile_md)
        if not profile_md:
            return result
        settings.personal_profile_md = profile_md
        await session.flush()
        return result

    async def list_memories(self, session: AsyncSession, user_id: int, limit: int = 10) -> list[Memory]:
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id)
            .order_by(desc(Memory.created_at))
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    async def delete_memory(self, session: AsyncSession, user_id: int, memory_id: int) -> bool:
        memory = await session.get(Memory, memory_id)
        if memory is None or memory.user_id != user_id:
            return False
        await session.delete(memory)
        await session.flush()
        return True

    async def clear_personal_profile(self, session: AsyncSession, user_id: int) -> bool:
        settings = await self._get_or_create_settings(session, user_id)
        had_profile = bool(settings.personal_profile_md.strip())
        settings.personal_profile_md = ""
        await session.flush()
        return had_profile

    async def retrieve_relevant(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        requester_user_id: int,
        guild_id: int | None,
        query: str,
        query_embedding: list[float] | None = None,
        generate_embedding: bool = True,
        limit: int | None = None,
        include_history: bool = False,
    ) -> list[Memory]:
        if not await self.is_enabled(session, user_id):
            return []
        cleaned_query = query.strip()
        if generate_embedding:
            query_embedding = await self.build_retrieval_query_embedding(
                session,
                query=cleaned_query,
                guild_id=guild_id,
                usage_user_id=user_id,
            )
        enabled_owner_ids = await self.get_enabled_user_ids(session, user_ids=None)
        memories = await self.retriever.retrieve(
            session,
            requester_user_id=requester_user_id,
            guild_id=guild_id,
            query=cleaned_query,
            query_embedding=query_embedding,
            owner_user_ids=enabled_owner_ids,
            limit=limit,
            include_history=include_history,
        )
        await session.flush()
        return memories

    async def retrieve_relevant_for_users(
        self,
        session: AsyncSession,
        *,
        user_ids: Iterable[int],
        requester_user_id: int,
        guild_id: int | None,
        query: str,
        usage_user_id: int | None,
        query_embedding: list[float] | None = None,
        generate_embedding: bool = True,
        limit: int | None = None,
        include_history: bool = False,
    ) -> dict[int, list[Memory]]:
        unique_user_ids = list(dict.fromkeys(user_ids))
        if not unique_user_ids:
            return {}
        enabled_user_ids = await self.get_enabled_user_ids(
            session,
            user_ids=unique_user_ids,
        )
        if not enabled_user_ids:
            return {}
        cleaned_query = query.strip()
        if generate_embedding:
            query_embedding = await self.build_retrieval_query_embedding(
                session,
                query=cleaned_query,
                guild_id=guild_id,
                usage_user_id=usage_user_id,
            )
        results: dict[int, list[Memory]] = {}
        for target_user_id in enabled_user_ids:
            results[target_user_id] = await self.retriever.retrieve(
                session,
                requester_user_id=requester_user_id,
                guild_id=guild_id,
                query=cleaned_query,
                query_embedding=query_embedding,
                owner_user_ids=(target_user_id,),
                limit=limit,
                include_history=include_history,
            )
        await session.flush()
        return results

    async def search_memories(
        self,
        session: AsyncSession,
        *,
        requester_user_id: int,
        guild_id: int | None,
        query: str,
        owner_user_ids: Iterable[int] | None = None,
        visibility_scopes: Iterable[MemoryVisibility | str] | None = None,
        query_embedding: list[float] | None = None,
        generate_embedding: bool = True,
        limit: int | None = None,
        include_history: bool | None = None,
    ) -> list[Memory]:
        """Search memories visible to a requester; suitable for a model-callable read tool."""

        if not await self.is_enabled(session, requester_user_id):
            return []
        cleaned_query = query.strip()
        if not cleaned_query:
            return []
        normalized_scopes = (
            tuple(normalize_memory_visibility(scope) for scope in visibility_scopes)
            if visibility_scopes is not None
            else tuple(MemoryVisibility)
        )
        if not normalized_scopes:
            return []
        requested_owner_ids = (
            tuple(dict.fromkeys(int(owner_user_id) for owner_user_id in owner_user_ids))
            if owner_user_ids is not None
            else None
        )
        enabled_owner_ids = await self.get_enabled_user_ids(
            session,
            user_ids=requested_owner_ids,
        )
        if not enabled_owner_ids:
            return []
        if generate_embedding:
            query_embedding = await self.build_retrieval_query_embedding(
                session,
                query=cleaned_query,
                guild_id=guild_id,
                usage_user_id=requester_user_id,
            )
        memories = await self.retriever.retrieve(
            session,
            requester_user_id=requester_user_id,
            guild_id=guild_id,
            query=cleaned_query,
            query_embedding=query_embedding,
            owner_user_ids=enabled_owner_ids,
            visibility_scopes=normalized_scopes,
            limit=limit,
            include_history=(
                include_history
                if include_history is not None
                else build_memory_retrieval_plan(
                    cleaned_query,
                    maximum=self.retriever.settings.memory_retrieval_limit,
                ).include_history
            ),
        )
        await session.flush()
        return memories

    async def build_retrieval_query_embedding(
        self,
        session: AsyncSession,
        *,
        query: str,
        guild_id: int | None,
        usage_user_id: int | None,
    ) -> list[float] | None:
        cleaned_query = query.strip()
        if not self.embedding_model or not cleaned_query:
            return None
        try:
            embedding_result = await self.llm_client.create_embedding(
                model=self.embedding_model,
                feature="memory_retrieve_embed",
                text=cleaned_query,
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Query embedding generation failed; falling back to lexical memory retrieval.")
            return None
        await record_usage(
            session,
            usage=embedding_result.usage,
            guild_id=guild_id,
            channel_id=None,
            user_id=usage_user_id,
        )
        return embedding_result.embedding

    async def maybe_store_memory(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
        visibility: MemoryVisibility | str | None = None,
    ) -> tuple[Memory | None, object | None]:
        if not await self.is_enabled(session, user_id):
            return None, None
        candidate, llm_result = await self.extractor.extract(
            current_message=current_message,
            recent_context=recent_context,
        )
        if candidate is None:
            return None, llm_result
        now = datetime.now(timezone.utc)
        selected_visibility: MemoryVisibility | str = (
            getattr(candidate, "suggested_visibility", MemoryVisibility.PRIVATE)
            if visibility is None and guild_id is not None
            else visibility or MemoryVisibility.PRIVATE
        )
        normalized_visibility = validate_memory_visibility_context(
            selected_visibility,
            guild_id=guild_id,
        )
        memory_kind = getattr(candidate, "memory_kind", MemoryKind.FACT)
        if not isinstance(memory_kind, MemoryKind):
            try:
                memory_kind = MemoryKind(str(memory_kind))
            except ValueError:
                memory_kind = MemoryKind.FACT
        subject_key = build_subject_key(
            user_id=user_id,
            guild_id=guild_id,
            kind=memory_kind,
            visibility=normalized_visibility.value,
        )
        predicate = str(getattr(candidate, "predicate", "") or "general_fact")[:64]
        operation = getattr(candidate, "operation", MemoryOperation.UPSERT)
        if not isinstance(operation, MemoryOperation):
            try:
                operation = MemoryOperation(str(operation))
            except ValueError:
                operation = MemoryOperation.UPSERT
        current = await self._find_active_typed_memory(
            session,
            user_id=user_id,
            guild_id=guild_id,
            visibility=normalized_visibility,
            memory_kind=memory_kind,
            subject_key=subject_key,
            predicate=predicate,
        )
        if operation is MemoryOperation.RETRACT:
            if current is None:
                return None, llm_result
            current.status = MemoryStatus.RETRACTED.value
            current.valid_until = now
            current.updated_at = now
            await session.flush()
            return current, llm_result

        object_text = str(getattr(candidate, "object_text", "") or candidate.summary).strip()[:180]
        related_entities = list(getattr(candidate, "related_entities", ()) or ())[:6]
        if current is not None and self._same_memory_value(current, object_text, candidate.summary):
            current.summary = candidate.summary or current.summary
            current.object_text = object_text or current.object_text
            current.confidence = min(max(current.confidence, candidate.confidence) + 0.04, 1.0)
            current.reinforcement_count = max(current.reinforcement_count or 1, 1) + 1
            current.last_confirmed_at = now
            current.updated_at = now
            current.tags = list(
                dict.fromkeys([*(current.tags or []), *candidate.tags])
            )[:5]
            current.related_entities = list(
                dict.fromkeys([*(current.related_entities or []), *related_entities])
            )[:6]
            await session.flush()
            return current, llm_result

        supersedes_id: int | None = None
        if current is not None:
            current.status = MemoryStatus.SUPERSEDED.value
            current.valid_until = now
            current.updated_at = now
            supersedes_id = current.id
        candidate_embedding: list[float] | None = None
        cleaned_summary = candidate.summary.strip()
        if self.embedding_model and cleaned_summary:
            try:
                embedding_result = await self.llm_client.create_embedding(
                    model=self.embedding_model,
                    feature="memory_store_embed",
                    text=cleaned_summary,
                )
            except Exception:  # pragma: no cover - defensive provider fallback
                LOGGER.exception("Memory embedding generation failed; storing memory without embedding.")
            else:
                candidate_embedding = embedding_result.embedding
                await record_usage(
                    session,
                    usage=embedding_result.usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
        duplicate = await self._find_duplicate(
            session,
            user_id=user_id,
            guild_id=guild_id,
            visibility=normalized_visibility,
            memory_kind=memory_kind,
            summary=candidate.summary,
        )
        if duplicate is not None:
            duplicate.memory_kind = memory_kind.value
            duplicate.status = MemoryStatus.ACTIVE.value
            duplicate.subject_key = subject_key
            duplicate.predicate = predicate
            duplicate.object_text = object_text
            duplicate.confidence = min(max(duplicate.confidence, candidate.confidence) + 0.04, 1.0)
            duplicate.reinforcement_count = max(duplicate.reinforcement_count or 1, 1) + 1
            duplicate.last_confirmed_at = now
            duplicate.updated_at = now
            duplicate.related_entities = list(
                dict.fromkeys([*(duplicate.related_entities or []), *related_entities])
            )[:6]
            duplicate.tags = list(
                dict.fromkeys([*(duplicate.tags or []), *candidate.tags])
            )[:5]
            if candidate_embedding and duplicate.embedding is None:
                duplicate.embedding = candidate_embedding
                duplicate.embedding_model = self.embedding_model
            await session.flush()
            return duplicate, llm_result

        ttl_days = getattr(candidate, "ttl_days", None)
        expires_at = (
            now + timedelta(days=ttl_days)
            if memory_kind is MemoryKind.WORKING and isinstance(ttl_days, int)
            else None
        )

        memory = Memory(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            visibility=normalized_visibility.value,
            category=candidate.category,
            memory_kind=memory_kind.value,
            status=MemoryStatus.ACTIVE.value,
            subject_key=subject_key,
            predicate=predicate,
            object_text=object_text,
            summary=candidate.summary,
            source_excerpt=candidate.source_excerpt,
            tags=candidate.tags,
            embedding=candidate_embedding,
            embedding_model=self.embedding_model,
            confidence=candidate.confidence,
            reinforcement_count=1,
            consolidation_count=0,
            related_entities=related_entities,
            supersedes_id=supersedes_id,
            valid_from=now,
            expires_at=expires_at,
            last_confirmed_at=now,
            updated_at=now,
        )
        session.add(memory)
        await session.flush()
        return memory, llm_result

    async def maybe_consolidate_memories(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime | None = None,
    ) -> tuple[Memory | None, LLMResult | None]:
        """Build one bounded background overview without replacing source facts."""

        current_time = now or datetime.now(timezone.utc)
        cooldown = getattr(self.extractor.settings, "memory_consolidation_cooldown_seconds", 21600)
        state_key = f"{MEMORY_CONSOLIDATION_STATE_PREFIX}:{user_id}"
        state = await session.get(AppState, state_key)
        if state is not None:
            try:
                last_run = datetime.fromisoformat(state.value)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                if (current_time - last_run.astimezone(timezone.utc)).total_seconds() < cooldown:
                    return None, None
            except ValueError:
                pass

        minimum = getattr(self.extractor.settings, "memory_consolidation_min_memories", 6)
        candidates = list(
            (
                await session.scalars(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        Memory.visibility == MemoryVisibility.PRIVATE.value,
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.memory_kind.in_(
                            [MemoryKind.FACT.value, MemoryKind.EPISODE.value]
                        ),
                    )
                    .order_by(desc(Memory.updated_at), desc(Memory.created_at))
                    .limit(12)
                )
            ).all()
        )
        if len(candidates) < minimum:
            return None, None
        availability_check = getattr(self.llm_client, "is_model_available", None)
        if callable(availability_check) and not availability_check(
            self.extractor.settings.openai_memory_model
        ):
            return None, None

        memory_lines = "\n".join(
            f"- id={memory.id}; kind={memory.memory_kind}; predicate={memory.predicate or '(legacy)'}; "
            f"summary={memory.summary}"
            for memory in candidates
        )
        try:
            result = await self.llm_client.complete_chat(
                model=self.extractor.settings.openai_memory_model,
                feature="memory_consolidate",
                max_tokens=320,
                temperature=0,
                request_timeout_seconds=8.0,
                request_max_retries=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Consolidate durable memory facts into one compact overview only when at least two facts "
                            "form a coherent, useful picture. Do not infer new facts, merge contradictions, include "
                            "sensitive details, or erase source facts. Return JSON only with keys: "
                            "should_consolidate, summary, source_ids, related_entities."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Active memories for user_id={user_id}:\n{memory_lines}\n\n"
                            "Keep summary under 280 characters and choose 2-8 source IDs from this list."
                        ),
                    },
                ],
            )
        except Exception as exc:  # defensive optional enrichment
            LOGGER.warning(
                "Memory consolidation deferred after provider failure: %s",
                " ".join(str(exc).split())[:240],
            )
            return None, None
        await self._touch_consolidation_state(session, key=state_key, when=current_time)
        payload = parse_json_object_payload(result.text)
        if not payload or not coerce_json_bool(payload.get("should_consolidate")):
            return None, result
        summary = " ".join(str(payload.get("summary", "")).split())[:280]
        if (
            not summary
            or contains_sensitive_pattern(summary)
            or contains_transient_memory_pattern(summary)
        ):
            return None, result
        allowed_ids = {memory.id for memory in candidates}
        source_ids = [
            source_id
            for value in payload.get("source_ids", [])
            if isinstance(value, int) and not isinstance(value, bool)
            if (source_id := int(value)) in allowed_ids
        ]
        source_ids = list(dict.fromkeys(source_ids))[:8]
        if len(source_ids) < 2:
            return None, result
        related_entities = list(normalize_related_entities(payload.get("related_entities")))
        existing = await self._find_active_typed_memory(
            session,
            user_id=user_id,
            guild_id=guild_id,
            visibility=MemoryVisibility.PRIVATE,
            memory_kind=MemoryKind.SUMMARY,
            subject_key=f"user:{user_id}",
            predicate="consolidated_context",
        )
        supersedes_id = None
        consolidation_count = 1
        if existing is not None:
            if existing.source_memory_ids == source_ids and existing.summary == summary:
                existing.last_confirmed_at = current_time
                existing.reinforcement_count += 1
                await session.flush()
                return existing, result
            existing.status = MemoryStatus.CONSOLIDATED.value
            existing.valid_until = current_time
            existing.updated_at = current_time
            supersedes_id = existing.id
            consolidation_count = (existing.consolidation_count or 0) + 1
        consolidated = Memory(
            guild_id=guild_id,
            user_id=user_id,
            visibility=MemoryVisibility.PRIVATE.value,
            category="project",
            memory_kind=MemoryKind.SUMMARY.value,
            status=MemoryStatus.ACTIVE.value,
            subject_key=f"user:{user_id}",
            predicate="consolidated_context",
            object_text=summary,
            summary=summary,
            source_excerpt=None,
            tags=["consolidated"],
            confidence=min(
                sum(memory.confidence for memory in candidates if memory.id in source_ids)
                / len(source_ids),
                1.0,
            ),
            reinforcement_count=1,
            consolidation_count=consolidation_count,
            related_entities=related_entities,
            source_memory_ids=source_ids,
            supersedes_id=supersedes_id,
            valid_from=current_time,
            last_confirmed_at=current_time,
            updated_at=current_time,
        )
        session.add(consolidated)
        await session.flush()
        return consolidated, result

    async def set_memory_visibility(
        self,
        session: AsyncSession,
        *,
        requester_user_id: int,
        memory_id: int,
        visibility: MemoryVisibility | str,
        guild_id: int | None,
    ) -> Memory | None:
        """Apply an owner-only visibility change after an outer confirmation boundary."""

        memory = await session.get(Memory, memory_id)
        if memory is None or memory.user_id != requester_user_id:
            return None
        normalized_visibility = validate_memory_visibility_context(
            visibility,
            guild_id=memory.guild_id,
        )
        if normalized_visibility is not MemoryVisibility.PRIVATE:
            if guild_id is None or memory.guild_id != guild_id:
                return None
        memory.visibility = normalized_visibility.value
        try:
            memory_kind = MemoryKind(memory.memory_kind)
        except ValueError:
            memory_kind = MemoryKind.FACT
        if normalized_visibility is MemoryVisibility.LORE:
            memory_kind = MemoryKind.LORE
            memory.memory_kind = memory_kind.value
        memory.subject_key = build_subject_key(
            user_id=memory.user_id,
            guild_id=memory.guild_id,
            kind=memory_kind,
            visibility=normalized_visibility.value,
        )
        memory.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return memory

    async def _get_or_create_settings(self, session: AsyncSession, user_id: int) -> UserSettings:
        stmt = select(UserSettings).where(UserSettings.user_id == user_id)
        settings = await session.scalar(stmt)
        if settings is not None:
            return settings
        settings = UserSettings(
            user_id=user_id,
            memory_enabled=False,
            timezone_name=DEFAULT_TIMEZONE_NAME,
        )
        session.add(settings)
        await session.flush()
        return settings

    async def _find_duplicate(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        visibility: MemoryVisibility,
        memory_kind: MemoryKind,
        summary: str,
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.visibility == visibility.value,
            Memory.status == ACTIVE_MEMORY_STATUS,
            Memory.memory_kind == memory_kind.value,
            func.lower(Memory.summary) == summary.lower(),
        )
        if visibility is not MemoryVisibility.PRIVATE:
            stmt = stmt.where(Memory.guild_id == guild_id)
        return await session.scalar(stmt)

    async def _find_active_typed_memory(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        visibility: MemoryVisibility,
        memory_kind: MemoryKind,
        subject_key: str,
        predicate: str,
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.visibility == visibility.value,
            Memory.status == ACTIVE_MEMORY_STATUS,
            Memory.memory_kind == memory_kind.value,
            Memory.subject_key == subject_key,
            Memory.predicate == predicate,
        )
        if visibility is not MemoryVisibility.PRIVATE:
            stmt = stmt.where(Memory.guild_id == guild_id)
        return await session.scalar(stmt.order_by(desc(Memory.updated_at), desc(Memory.created_at)))

    @staticmethod
    def _same_memory_value(memory: Memory, object_text: str, summary: str) -> bool:
        existing_value = " ".join((memory.object_text or memory.summary).casefold().split())
        candidate_value = " ".join((object_text or summary).casefold().split())
        return bool(existing_value and candidate_value and existing_value == candidate_value)

    @staticmethod
    async def _touch_consolidation_state(
        session: AsyncSession,
        *,
        key: str,
        when: datetime,
    ) -> None:
        state = await session.get(AppState, key)
        value = when.astimezone(timezone.utc).isoformat()
        if state is None:
            session.add(AppState(key=key, value=value))
        else:
            state.value = value
        await session.flush()

    async def get_enabled_user_ids(
        self,
        session: AsyncSession,
        *,
        user_ids: Iterable[int] | None,
    ) -> tuple[int, ...]:
        stmt = select(UserSettings.user_id).where(UserSettings.memory_enabled.is_(True))
        if user_ids is not None:
            normalized_user_ids = tuple(dict.fromkeys(user_ids))
            if not normalized_user_ids:
                return ()
            stmt = stmt.where(UserSettings.user_id.in_(normalized_user_ids))
        return tuple((await session.scalars(stmt)).all())

    async def prune_stale_memories(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        never_retrieved_older_than_days: int,
        stale_retrieved_older_than_days: int,
    ) -> int:
        created_cutoff = now - timedelta(days=max(never_retrieved_older_than_days, 1))
        retrieved_cutoff = now - timedelta(days=max(stale_retrieved_older_than_days, 1))
        await session.execute(
            update(Memory)
            .where(
                Memory.status == ACTIVE_MEMORY_STATUS,
                or_(
                    and_(Memory.expires_at.is_not(None), Memory.expires_at <= now),
                    and_(Memory.valid_until.is_not(None), Memory.valid_until <= now),
                ),
            )
            .values(status=MemoryStatus.EXPIRED.value, updated_at=now)
        )
        result = await session.execute(
            delete(Memory).where(
                or_(
                    and_(
                        Memory.status != ACTIVE_MEMORY_STATUS,
                        func.coalesce(Memory.updated_at, Memory.created_at) < retrieved_cutoff,
                    ),
                    and_(
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.times_retrieved <= 0,
                        Memory.last_retrieved_at.is_(None),
                        Memory.created_at < created_cutoff,
                    ),
                    and_(
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.last_retrieved_at.is_not(None),
                        Memory.last_retrieved_at < retrieved_cutoff,
                    ),
                )
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def format_memory_list(memories: list[Memory]) -> str:
        if not memories:
            return "No stored memories yet."
        lines = []
        for memory in memories:
            summary = memory.summary if len(memory.summary) <= 110 else f"{memory.summary[:107]}..."
            memory_kind = getattr(memory, "memory_kind", MemoryKind.FACT.value)
            status = getattr(memory, "status", MemoryStatus.ACTIVE.value)
            lines.append(
                f"`{memory.id}` [{memory_kind}; {status}; {memory.visibility}] {summary} "
                f"(confidence {memory.confidence:.2f})"
            )
        return "\n".join(lines)
