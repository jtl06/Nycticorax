from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.config import Settings
from nycti.db.models import Memory
from nycti.memory.filtering import lexical_similarity
from nycti.memory.scoring import cosine_similarity
from nycti.memory.visibility import (
    GUILD_VISIBLE_MEMORY_SCOPES,
    MemoryVisibility,
    can_read_memory,
    normalize_memory_visibility,
)

MIN_LEXICAL_SIGNAL = 0.05
MIN_SEMANTIC_SIGNAL = 0.25
LEXICAL_WEIGHT = 0.28
SEMANTIC_WEIGHT = 0.72
MAX_CANDIDATES_PER_ACCESS_POOL = 125


class MemoryRetriever:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        requester_user_id: int,
        guild_id: int | None,
        query: str,
        query_embedding: list[float] | None = None,
        owner_user_ids: Iterable[int] | None = None,
        visibility_scopes: Iterable[MemoryVisibility | str] | None = None,
    ) -> list[Memory]:
        requested_scopes = self._normalize_visibility_scopes(visibility_scopes)
        if not requested_scopes:
            return []
        normalized_owner_ids = (
            tuple(dict.fromkeys(int(owner_user_id) for owner_user_id in owner_user_ids))
            if owner_user_ids is not None
            else None
        )
        if normalized_owner_ids is not None and not normalized_owner_ids:
            return []

        candidate_statements = []
        private_owner_requested = (
            normalized_owner_ids is None
            or requester_user_id in normalized_owner_ids
        )
        if MemoryVisibility.PRIVATE in requested_scopes and private_owner_requested:
            private_stmt = (
                select(Memory)
                .where(
                    Memory.visibility == MemoryVisibility.PRIVATE.value,
                    Memory.user_id == requester_user_id,
                )
                .order_by(desc(Memory.created_at))
                .limit(MAX_CANDIDATES_PER_ACCESS_POOL)
            )
            if normalized_owner_ids is not None:
                private_stmt = private_stmt.where(
                    Memory.user_id.in_(normalized_owner_ids)
                )
            candidate_statements.append(private_stmt)
        guild_scopes = requested_scopes.intersection(GUILD_VISIBLE_MEMORY_SCOPES)
        if guild_id is not None and guild_scopes:
            guild_stmt = (
                select(Memory)
                .where(
                    Memory.guild_id == guild_id,
                    Memory.visibility.in_(scope.value for scope in guild_scopes),
                )
                .order_by(desc(Memory.created_at))
                .limit(MAX_CANDIDATES_PER_ACCESS_POOL)
            )
            if normalized_owner_ids is not None:
                guild_stmt = guild_stmt.where(
                    Memory.user_id.in_(normalized_owner_ids)
                )
            candidate_statements.append(guild_stmt)
        if not candidate_statements:
            return []

        memories_by_identity: dict[int, Memory] = {}
        for statement in candidate_statements:
            for memory in (await session.scalars(statement)).all():
                # SQLAlchemy's identity map returns the same object when a row
                # belongs to more than one query. Object identity also keeps
                # lightweight fake-session tests deterministic.
                memories_by_identity[id(memory)] = memory
        memories = list(memories_by_identity.values())
        if not memories:
            return []

        now = datetime.now(timezone.utc)
        ranked: list[tuple[float, float, Memory]] = []
        for memory in memories:
            raw_visibility = getattr(memory, "visibility", MemoryVisibility.PRIVATE.value)
            try:
                memory_visibility = normalize_memory_visibility(raw_visibility)
            except ValueError:
                continue
            if memory_visibility not in requested_scopes:
                continue
            if normalized_owner_ids is not None and memory.user_id not in normalized_owner_ids:
                continue
            if not can_read_memory(
                visibility=memory_visibility,
                owner_user_id=memory.user_id,
                memory_guild_id=memory.guild_id,
                requester_user_id=requester_user_id,
                requester_guild_id=guild_id,
            ):
                continue
            lexical_relevance = lexical_similarity(query, memory.summary, memory.tags)
            semantic_similarity = cosine_similarity(query_embedding, memory.embedding)
            has_lexical_signal = lexical_relevance >= MIN_LEXICAL_SIGNAL
            has_semantic_signal = semantic_similarity >= MIN_SEMANTIC_SIGNAL
            if not has_lexical_signal and not has_semantic_signal:
                continue
            weighted_relevance = (
                (semantic_similarity * SEMANTIC_WEIGHT if has_semantic_signal else 0.0)
                + (lexical_relevance * LEXICAL_WEIGHT if has_lexical_signal else 0.0)
            )
            qualifying_weight = (
                (SEMANTIC_WEIGHT if has_semantic_signal else 0.0)
                + (LEXICAL_WEIGHT if has_lexical_signal else 0.0)
            )
            relevance_score = weighted_relevance / qualifying_weight
            age_days = max((now - _as_utc(memory.created_at)).days, 0)
            recency_bonus = max(0.0, 0.12 - (age_days * 0.002))
            category_bonus = 0.08 if memory.category == "preference" else 0.0
            confidence_bonus = min(memory.confidence, 1.0) * 0.08
            prior_score = recency_bonus + category_bonus + confidence_bonus
            ranked.append((relevance_score, prior_score, memory))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [memory for _, _, memory in ranked][: self.settings.memory_retrieval_limit]

        for memory in selected:
            memory.times_retrieved += 1
            memory.last_retrieved_at = now

        return selected

    @staticmethod
    def _normalize_visibility_scopes(
        visibility_scopes: Iterable[MemoryVisibility | str] | None,
    ) -> frozenset[MemoryVisibility]:
        if visibility_scopes is None:
            return frozenset(MemoryVisibility)
        return frozenset(normalize_memory_visibility(scope) for scope in visibility_scopes)


def _as_utc(value: datetime) -> datetime:
    """Normalize timestamps loaded by SQLite, which discards timezone metadata."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
