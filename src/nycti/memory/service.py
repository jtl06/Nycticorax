from __future__ import annotations

import logging

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Memory, UserSettings
from nycti.llm.client import OpenAIClient
from nycti.memory.extractor import MemoryCandidate, MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
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

    async def retrieve_relevant(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        query: str,
    ) -> list[Memory]:
        if not await self.is_enabled(session, user_id):
            return []
        cleaned_query = query.strip()
        query_embedding: list[float] | None = None
        if self.embedding_model and cleaned_query:
            try:
                embedding_result = await self.llm_client.create_embedding(
                    model=self.embedding_model,
                    feature="memory_retrieve_embed",
                    text=cleaned_query,
                )
            except Exception:  # pragma: no cover - defensive provider fallback
                LOGGER.exception("Query embedding generation failed; falling back to lexical memory retrieval.")
            else:
                query_embedding = embedding_result.embedding
                await record_usage(
                    session,
                    usage=embedding_result.usage,
                    guild_id=guild_id,
                    channel_id=None,
                    user_id=user_id,
                )
        memories = await self.retriever.retrieve(
            session,
            user_id=user_id,
            guild_id=guild_id,
            query=cleaned_query,
            query_embedding=query_embedding,
        )
        await session.flush()
        return memories

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
    ) -> tuple[Memory | None, object | None]:
        if not await self.is_enabled(session, user_id):
            return None, None

        candidate, llm_result = await self.extractor.extract(
            current_message=current_message,
            recent_context=recent_context,
        )
        if candidate is None:
            return None, llm_result

        duplicate = await self._find_duplicate(session, user_id=user_id, summary=candidate.summary)
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

    async def _get_or_create_settings(self, session: AsyncSession, user_id: int) -> UserSettings:
        stmt = select(UserSettings).where(UserSettings.user_id == user_id)
        settings = await session.scalar(stmt)
        if settings is not None:
            return settings
        settings = UserSettings(
            user_id=user_id,
            memory_enabled=True,
            timezone_name=DEFAULT_TIMEZONE_NAME,
        )
        session.add(settings)
        await session.flush()
        return settings

    async def _find_duplicate(
        self, session: AsyncSession, *, user_id: int, summary: str
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            func.lower(Memory.summary) == summary.lower(),
        )
        return await session.scalar(stmt)

    @staticmethod
    def format_memory_list(memories: list[Memory]) -> str:
        if not memories:
            return "No stored memories yet."
        lines = []
        for memory in memories:
            summary = memory.summary if len(memory.summary) <= 110 else f"{memory.summary[:107]}..."
            lines.append(
                f"`{memory.id}` [{memory.category}] {summary} "
                f"(confidence {memory.confidence:.2f})"
            )
        return "\n".join(lines)
