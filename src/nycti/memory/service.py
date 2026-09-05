from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Memory, MemorySnapshot, UserSettings
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.llm.types import EmbeddingResult
from nycti.memory.consolidation import (
    MemoryConsolidationDecision as MemoryConsolidationDecision,
    MemoryConsolidationMixin,
    MemoryConsolidationPlan as MemoryConsolidationPlan,
    MemoryConsolidationSource as MemoryConsolidationSource,
)
from nycti.memory.extractor import (
    MemoryCandidate,
    MemoryExtractor,
)
from nycti.memory.profile import (
    clean_profile_markdown,
    strip_noncaller_profile_lines,
    strip_sensitive_profile_lines,
)
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.snapshot_service import (
    ActiveMarketWatchlist as ActiveMarketWatchlist,
    MemorySnapshotBlocks as MemorySnapshotBlocks,
    MemorySnapshotMixin,
)
from nycti.memory.filtering import contains_sensitive_pattern
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    MemoryOperation,
    MemoryStatus,
    build_memory_retrieval_plan,
    build_subject_key,
)
from nycti.memory.visibility import (
    MemoryVisibility,
    normalize_memory_visibility,
    validate_memory_visibility_context,
)
from nycti.timezones import DEFAULT_TIMEZONE_NAME, resolve_timezone_name
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
DURABLE_MEMORY_RETENTION_MULTIPLIER = 2


@dataclass(frozen=True, slots=True)
class MemoryPromptSettings:
    timezone_name: str
    memory_enabled: bool
    personal_profile_md: str


class MemoryService(MemorySnapshotMixin, MemoryConsolidationMixin):
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
        guild_ids = {
            int(guild_id)
            for guild_id in (
                await session.scalars(
                    select(Memory.guild_id)
                    .where(Memory.user_id == user_id, Memory.guild_id.is_not(None))
                    .distinct()
                )
            ).all()
            if guild_id is not None
        }
        await session.execute(
            delete(MemorySnapshot).where(MemorySnapshot.user_id == user_id)
        )
        current_time = datetime.now(timezone.utc)
        if enabled:
            user_guild_ids: set[int | None] = set(guild_ids)
            user_guild_ids.add(None)
            for scoped_guild_id in sorted(
                user_guild_ids,
                key=lambda item: item or 0,
            ):
                await self._rebuild_user_memory_snapshot(
                    session,
                    user_id=user_id,
                    guild_id=scoped_guild_id,
                    now=current_time,
                )
        for guild_id in sorted(guild_ids):
            await self._rebuild_guild_memory_snapshot(
                session,
                guild_id=guild_id,
                now=current_time,
            )
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

    async def get_prompt_settings(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> MemoryPromptSettings:
        """Load prompt-facing user settings with one database lookup."""
        settings = await self._get_or_create_settings(session, user_id)
        return MemoryPromptSettings(
            timezone_name=resolve_timezone_name(settings.timezone_name),
            memory_enabled=settings.memory_enabled,
            personal_profile_md=settings.personal_profile_md.strip(),
        )

    async def apply_personal_profile_update(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        profile_md: str,
        expected_profile: str | None = None,
    ) -> bool:
        """Persist an explicit profile edit after opt-in and optimistic-state checks."""

        settings = await self._get_or_create_settings(session, user_id)
        if not settings.memory_enabled:
            return False
        if (
            expected_profile is not None
            and settings.personal_profile_md.strip() != expected_profile.strip()
        ):
            return False
        cleaned_profile = strip_sensitive_profile_lines(
            strip_noncaller_profile_lines(clean_profile_markdown(profile_md))
        )
        if not cleaned_profile:
            return False
        settings.personal_profile_md = cleaned_profile
        await session.flush()
        return True

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
        guild_id = memory.guild_id
        await session.delete(memory)
        await session.flush()
        await self.rebuild_memory_snapshots(
            session,
            user_id=user_id,
            guild_id=guild_id,
        )
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
        memory_enabled: bool | None = None,
        limit: int | None = None,
        include_history: bool = False,
    ) -> list[Memory]:
        enabled = (
            await self.is_enabled(session, user_id)
            if memory_enabled is None
            else memory_enabled
        )
        if not enabled:
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
        embedding_result = await self.generate_retrieval_query_embedding(query=query)
        if embedding_result is None:
            return None
        await self.record_retrieval_query_embedding_usage(
            session,
            guild_id=guild_id,
            usage_user_id=usage_user_id,
            embedding_result=embedding_result,
        )
        return embedding_result.embedding

    async def generate_retrieval_query_embedding(
        self,
        *,
        query: str,
    ) -> EmbeddingResult | None:
        """Generate an embedding without touching a database session."""
        cleaned_query = query.strip()
        if not self.embedding_model or not cleaned_query:
            return None
        try:
            return await self.llm_client.create_embedding(
                model=self.embedding_model,
                feature="memory_retrieve_embed",
                text=cleaned_query,
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Query embedding generation failed; falling back to lexical memory retrieval.")
            return None

    @staticmethod
    async def record_retrieval_query_embedding_usage(
        session: AsyncSession,
        *,
        guild_id: int | None,
        usage_user_id: int | None,
        embedding_result: EmbeddingResult,
    ) -> None:
        await record_usage(
            session,
            usage=embedding_result.usage,
            guild_id=guild_id,
            channel_id=None,
            user_id=usage_user_id,
        )

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
        memory = await self.store_memory_candidate(
            session,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            source_message_id=source_message_id,
            candidate=candidate,
            visibility=visibility,
        )
        should_generate_embedding = bool(
            memory is not None
            and candidate.summary.strip()
            and await self.memory_embedding_target_is_current(
                session,
                memory_id=memory.id,
                user_id=user_id,
                expected_summary=candidate.summary,
            )
        )
        if should_generate_embedding and memory is not None:
            embedding_result = await self.generate_memory_storage_embedding(candidate.summary)
            if embedding_result is not None:
                await self.attach_memory_embedding(
                    session,
                    memory_id=memory.id,
                    user_id=user_id,
                    embedding=embedding_result.embedding,
                )
                await record_usage(
                    session,
                    usage=embedding_result.usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
        return memory, llm_result

    async def generate_memory_candidate(
        self,
        *,
        current_message: str,
        recent_context: str,
        correction_context: bool = False,
    ) -> tuple[MemoryCandidate | None, LLMResult | None]:
        """Classify one message without touching a database session."""

        return await self.extractor.extract(
            current_message=current_message,
            recent_context=recent_context,
            correction_context=correction_context,
        )

    async def generate_memory_candidates(
        self,
        *,
        current_message: str,
        recent_context: str,
        correction_context: bool = False,
    ) -> tuple[list[MemoryCandidate], LLMResult | None]:
        """Classify one message into one or more independently keyed facts."""

        extract_many = getattr(self.extractor, "extract_many", None)
        if callable(extract_many):
            return await extract_many(
                current_message=current_message,
                recent_context=recent_context,
                correction_context=correction_context,
            )
        candidate, result = await self.generate_memory_candidate(
            current_message=current_message,
            recent_context=recent_context,
            correction_context=correction_context,
        )
        return ([candidate] if candidate is not None else []), result

    async def generate_memory_storage_embedding(
        self,
        summary: str,
    ) -> EmbeddingResult | None:
        """Generate a storage embedding without holding a database session."""

        cleaned_summary = summary.strip()
        if not self.embedding_model or not cleaned_summary:
            return None
        try:
            return await self.llm_client.create_embedding(
                model=self.embedding_model,
                feature="memory_store_embed",
                text=cleaned_summary,
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Memory embedding generation failed; storing memory without embedding.")
            return None

    async def attach_memory_embedding(
        self,
        session: AsyncSession,
        *,
        memory_id: int,
        user_id: int,
        embedding: list[float],
    ) -> bool:
        if not await self.is_enabled(session, user_id):
            return False
        memory = await session.get(Memory, memory_id)
        if (
            memory is None
            or memory.user_id != user_id
            or memory.status != ACTIVE_MEMORY_STATUS
            or memory.embedding is not None
        ):
            return False
        memory.embedding = embedding
        memory.embedding_model = self.embedding_model
        await session.flush()
        return True

    async def memory_embedding_target_is_current(
        self,
        session: AsyncSession,
        *,
        memory_id: int,
        user_id: int,
        expected_summary: str,
    ) -> bool:
        """Revalidate an embedding target immediately before provider work."""

        if not await self.is_enabled(session, user_id):
            return False
        memory = await session.get(Memory, memory_id)
        return bool(
            memory is not None
            and memory.user_id == user_id
            and memory.status == ACTIVE_MEMORY_STATUS
            and memory.embedding is None
            and memory.summary == expected_summary
        )

    async def store_memory_candidate(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        candidate: MemoryCandidate,
        visibility: MemoryVisibility | str | None = None,
        candidate_embedding: list[float] | None = None,
    ) -> Memory | None:
        """Persist an already classified candidate without external provider calls."""

        candidate_text = "\n".join(
            (
                candidate.summary,
                candidate.source_excerpt,
                str(candidate.object_text or ""),
            )
        )
        if contains_sensitive_pattern(candidate_text):
            return None
        if not await self.is_enabled(session, user_id):
            return None
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
                return None
            current.status = MemoryStatus.RETRACTED.value
            current.valid_until = now
            current.updated_at = now
            await session.flush()
            return current

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
            return current

        supersedes_id: int | None = None
        if current is not None:
            current.status = MemoryStatus.SUPERSEDED.value
            current.valid_until = now
            current.updated_at = now
            supersedes_id = current.id
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
            return duplicate

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
        return memory

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
        await self.rebuild_memory_snapshots(
            session,
            user_id=memory.user_id,
            guild_id=memory.guild_id,
        )
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
        never_retrieved_days = max(never_retrieved_older_than_days, 1)
        stale_retrieved_days = max(stale_retrieved_older_than_days, 1)
        created_cutoff = now - timedelta(days=never_retrieved_days)
        retrieved_cutoff = now - timedelta(days=stale_retrieved_days)
        durable_created_cutoff = now - timedelta(
            days=never_retrieved_days * DURABLE_MEMORY_RETENTION_MULTIPLIER
        )
        durable_retrieved_cutoff = now - timedelta(
            days=stale_retrieved_days * DURABLE_MEMORY_RETENTION_MULTIPLIER
        )
        durable_memory = or_(
            and_(
                Memory.memory_kind.in_(
                    [
                        MemoryKind.FACT.value,
                        MemoryKind.LORE.value,
                        MemoryKind.SUMMARY.value,
                    ]
                ),
                Memory.category.not_in(["plan", "routine"]),
            ),
            func.coalesce(Memory.reinforcement_count, 1) >= 2,
        )
        ordinary_memory = ~durable_memory
        memory_activity_at = func.coalesce(
            Memory.last_confirmed_at,
            Memory.updated_at,
            Memory.created_at,
        )
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
                        or_(
                            and_(durable_memory, memory_activity_at < durable_created_cutoff),
                            and_(ordinary_memory, memory_activity_at < created_cutoff),
                        ),
                    ),
                    and_(
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.last_retrieved_at.is_not(None),
                        or_(
                            and_(
                                durable_memory,
                                Memory.last_retrieved_at < durable_retrieved_cutoff,
                                memory_activity_at < durable_retrieved_cutoff,
                            ),
                            and_(
                                ordinary_memory,
                                Memory.last_retrieved_at < retrieved_cutoff,
                                memory_activity_at < retrieved_cutoff,
                            ),
                        ),
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
