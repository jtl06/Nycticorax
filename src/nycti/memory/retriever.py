from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.config import Settings
from nycti.db.models import Memory
from nycti.memory.filtering import lexical_similarity


class MemoryRetriever:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        query: str,
    ) -> list[Memory]:
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id)
            .order_by(desc(Memory.created_at))
            .limit(75)
        )
        if guild_id is not None:
            stmt = stmt.where(Memory.guild_id == guild_id)

        memories = list((await session.scalars(stmt)).all())
        if not memories:
            return []

        now = datetime.now(timezone.utc)
        ranked = []
        for memory in memories:
            relevance = lexical_similarity(query, memory.summary, memory.tags)
            age_days = max((now - memory.created_at).days, 0)
            recency_bonus = max(0.0, 0.12 - (age_days * 0.002))
            category_bonus = 0.08 if memory.category == "preference" else 0.0
            confidence_bonus = min(memory.confidence, 1.0) * 0.08
            score = relevance + recency_bonus + category_bonus + confidence_bonus
            ranked.append((score, memory))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = [memory for score, memory in ranked if score > 0.12][: self.settings.memory_retrieval_limit]

        for memory in selected:
            memory.times_retrieved += 1
            memory.last_retrieved_at = now

        return selected
