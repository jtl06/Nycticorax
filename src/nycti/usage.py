from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import MessageDebugEvent, ToolCallEvent, UsageEvent
from nycti.llm.client import LLMUsage


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
        provider=str(getattr(usage, "provider", "openai-default") or "openai-default")[:32],
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


async def record_tool_call(
    session: AsyncSession,
    *,
    tool_name: str,
    status: str,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    latency_ms: int,
) -> None:
    event = ToolCallEvent(
        tool_name=tool_name,
        status=status,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        latency_ms=max(latency_ms, 0),
    )
    session.add(event)
    await session.flush()


async def record_message_debug_stats(
    session: AsyncSession,
    *,
    metrics: dict[str, int | str],
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    source_message_id: int | None,
) -> None:
    events = [
        MessageDebugEvent(
            part=key,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            latency_ms=max(value, 0),
        )
        for key, value in metrics.items()
        if key.endswith("_ms") and isinstance(value, int)
    ]
    if not events:
        return
    session.add_all(events)
    await session.flush()


async def prune_usage_events_before(
    session: AsyncSession,
    *,
    cutoff: datetime,
) -> int:
    result = await session.execute(
        delete(UsageEvent).where(UsageEvent.created_at < cutoff)
    )
    return int(result.rowcount or 0)


async def prune_message_debug_events_before(
    session: AsyncSession,
    *,
    cutoff: datetime,
) -> int:
    result = await session.execute(
        delete(MessageDebugEvent).where(MessageDebugEvent.created_at < cutoff)
    )
    return int(result.rowcount or 0)
