from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import re

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.config import Settings
from nycti.db.models import Memory
from nycti.memory.filtering import lexical_similarity
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    effective_memory_confidence,
    memory_entity_overlap,
    memory_is_active,
)
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
MAX_CANDIDATES_PER_ACCESS_POOL = 300


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
        limit: int | None = None,
        include_history: bool = False,
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
        allowed_statuses = (
            [
                ACTIVE_MEMORY_STATUS,
                "superseded",
                "retracted",
                "consolidated",
            ]
            if include_history
            else [ACTIVE_MEMORY_STATUS]
        )
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
                    Memory.status.in_(allowed_statuses),
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
                    Memory.status.in_(allowed_statuses),
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
            is_active = memory_is_active(memory, now=now)
            if not is_active and not include_history:
                continue
            if not is_active and str(getattr(memory, "status", "")) == "expired":
                continue
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
            lexical_relevance = lexical_similarity(query, memory.summary, memory.tags or [])
            semantic_similarity = cosine_similarity(query_embedding, memory.embedding)
            entity_relevance = memory_entity_overlap(
                query,
                getattr(memory, "related_entities", None) or [],
            )
            ticker_relevance = _ticker_interest_relevance(query, memory)
            has_lexical_signal = lexical_relevance >= MIN_LEXICAL_SIGNAL
            has_semantic_signal = semantic_similarity >= MIN_SEMANTIC_SIGNAL
            has_entity_signal = entity_relevance > 0 or ticker_relevance > 0
            if not has_lexical_signal and not has_semantic_signal and not has_entity_signal:
                continue
            weighted_relevance = (
                (semantic_similarity * SEMANTIC_WEIGHT if has_semantic_signal else 0.0)
                + (lexical_relevance * LEXICAL_WEIGHT if has_lexical_signal else 0.0)
                + (max(entity_relevance, ticker_relevance) * 0.18 if has_entity_signal else 0.0)
            )
            qualifying_weight = (
                (SEMANTIC_WEIGHT if has_semantic_signal else 0.0)
                + (LEXICAL_WEIGHT if has_lexical_signal else 0.0)
                + (0.18 if has_entity_signal else 0.0)
            )
            relevance_score = weighted_relevance / qualifying_weight
            age_days = max((now - _as_utc(memory.created_at)).days, 0)
            recency_bonus = max(0.0, 0.12 - (age_days * 0.002))
            category_bonus = 0.08 if memory.category == "preference" else 0.0
            effective_confidence = effective_memory_confidence(
                memory,
                now=now,
                half_life_days=getattr(
                    self.settings,
                    "memory_confidence_half_life_days",
                    365,
                ),
            )
            confidence_bonus = effective_confidence * 0.08
            prior_score = recency_bonus + category_bonus + confidence_bonus
            if not is_active:
                prior_score -= 0.10
            ranked.append((relevance_score, prior_score, memory))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        effective_limit = min(
            max(limit or self.settings.memory_retrieval_limit, 1),
            self.settings.memory_retrieval_limit,
        )
        selected = self._select_diverse(ranked, limit=effective_limit)

        for memory in selected:
            memory.times_retrieved += 1
            memory.last_retrieved_at = now

        return selected

    @staticmethod
    def _select_diverse(
        ranked: list[tuple[float, float, Memory]],
        *,
        limit: int,
    ) -> list[Memory]:
        caps = {
            MemoryKind.SUMMARY.value: 1,
            MemoryKind.LORE.value: 1,
            MemoryKind.WORKING.value: 1,
            MemoryKind.EPISODE.value: 2,
        }
        selected: list[Memory] = []
        kind_counts: dict[str, int] = {}
        seen_keys: set[tuple[object, ...]] = set()
        for _, _, memory in ranked:
            kind = str(getattr(memory, "memory_kind", MemoryKind.FACT.value) or MemoryKind.FACT.value)
            key = (
                memory.user_id,
                getattr(memory, "subject_key", None),
                getattr(memory, "predicate", None),
                memory.summary.casefold(),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            cap = caps.get(kind, limit)
            if kind_counts.get(kind, 0) >= cap:
                continue
            selected.append(memory)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if len(selected) >= limit:
                return selected
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


def _ticker_interest_relevance(query: str, memory: Memory) -> float:
    predicate = str(getattr(memory, "predicate", "") or "")
    if not predicate.startswith("stock_ticker_interest_"):
        return 0.0
    symbol = str(getattr(memory, "object_text", "") or "").strip()
    if not symbol:
        return 0.0
    return float(
        re.search(
            rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])",
            query,
            re.IGNORECASE,
        )
        is not None
    )
