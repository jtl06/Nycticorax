from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Memory, UserSettings
from nycti.formatting import parse_json_object_payload
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.memory.extractor import MemoryExtractor, coerce_json_bool
from nycti.memory.profile import clean_profile_markdown, strip_noncaller_profile_lines
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.visibility import (
    MemoryVisibility,
    normalize_memory_visibility,
    validate_memory_visibility_context,
)
from nycti.timezones import DEFAULT_TIMEZONE_NAME, resolve_timezone_name
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)


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
        selected_visibility: MemoryVisibility | str = (
            getattr(candidate, "suggested_visibility", MemoryVisibility.PRIVATE)
            if visibility is None and guild_id is not None
            else visibility or MemoryVisibility.PRIVATE
        )
        normalized_visibility = validate_memory_visibility_context(
            selected_visibility,
            guild_id=guild_id,
        )
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
            summary=candidate.summary,
        )
        if duplicate is not None:
            duplicate.confidence = max(duplicate.confidence, candidate.confidence)
            duplicate.tags = list(dict.fromkeys([*duplicate.tags, *candidate.tags]))[:5]
            if candidate_embedding and duplicate.embedding is None:
                duplicate.embedding = candidate_embedding
                duplicate.embedding_model = self.embedding_model
            await session.flush()
            return duplicate, llm_result

        memory = Memory(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            visibility=normalized_visibility.value,
            category=candidate.category,
            summary=candidate.summary,
            source_excerpt=candidate.source_excerpt,
            tags=candidate.tags,
            embedding=candidate_embedding,
            embedding_model=self.embedding_model,
            confidence=candidate.confidence,
        )
        session.add(memory)
        await session.flush()
        return memory, llm_result

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
        summary: str,
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.visibility == visibility.value,
            func.lower(Memory.summary) == summary.lower(),
        )
        if visibility is not MemoryVisibility.PRIVATE:
            stmt = stmt.where(Memory.guild_id == guild_id)
        return await session.scalar(stmt)

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
        result = await session.execute(
            delete(Memory).where(
                or_(
                    and_(
                        Memory.times_retrieved <= 0,
                        Memory.last_retrieved_at.is_(None),
                        Memory.created_at < created_cutoff,
                    ),
                    and_(
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
            lines.append(
                f"`{memory.id}` [{memory.category}; {memory.visibility}] {summary} "
                f"(confidence {memory.confidence:.2f})"
            )
        return "\n".join(lines)
