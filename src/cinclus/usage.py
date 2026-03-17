from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cinclus.db.models import UsageEvent
from cinclus.llm.client import LLMUsage


async def record_usage(
    session: AsyncSession,
    *,
    usage: LLMUsage,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
) -> None:
    event = UsageEvent(
        feature=usage.feature,
        provider="openai",
        model=usage.model,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
    )
    session.add(event)
    await session.flush()
