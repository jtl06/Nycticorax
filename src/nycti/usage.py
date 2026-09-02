from __future__ import annotations

from datetime import datetime
from typing import Mapping

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import AgentRunEvent, AgentStepEvent, AppState, MessageDebugEvent, ToolCallEvent, UsageEvent
from nycti.llm.client import LLMUsage


ADDITIVE_TIMING_PARTS = (
    "timing_total_ms",
    "timing_model_ms",
    "timing_tools_ms",
    "timing_memory_ms",
    "timing_context_ms",
    "timing_send_ms",
    "timing_vision_ms",
    "timing_db_ms",
    "timing_other_ms",
)

CONTEXT_PROFILE_TIMING_PARTS = (
    "ctx_discord_ms",
    "ctx_recent_ms",
    "ctx_reply_ms",
    "ctx_links_ms",
    "ctx_anchor_ms",
    "ctx_msg_format_ms",
    "ctx_member_write_ms",
    "ctx_prepare_ms",
    "ctx_tz_ms",
    "ctx_mem_flag_ms",
    "ctx_profile_ms",
    "ctx_snapshot_ms",
    "ctx_watchlist_ms",
    "ctx_chan_alias_ms",
    "ctx_member_alias_ms",
    "ctx_member_ids_ms",
    "ctx_related_auth_ms",
    "ctx_embed_ms",
    "ctx_mem_query_ms",
    "ctx_related_mem_ms",
    "ctx_prepare_format_ms",
    "ctx_prompt_ms",
)


def build_additive_timing_metrics(
    metrics: Mapping[str, int | str],
) -> dict[str, int]:
    """Partition one request's wall time into non-overlapping phases."""

    total = _nonnegative_metric(metrics, "end_to_end_ms")
    if total <= 0:
        return {}

    context = min(_nonnegative_metric(metrics, "context_fetch_ms"), total)
    send = min(_nonnegative_metric(metrics, "reply_send_ms"), total - context)
    reply_budget = total - context - send
    raw_reply_phases = (
        ("timing_model_ms", _nonnegative_metric(metrics, "chat_llm_ms")),
        ("timing_tools_ms", _nonnegative_metric(metrics, "tool_execution_wall_ms")),
        ("timing_memory_ms", _nonnegative_metric(metrics, "memory_retrieval_ms")),
        ("timing_vision_ms", _nonnegative_metric(metrics, "vision_wait_ms")),
        ("timing_db_ms", _nonnegative_metric(metrics, "chat_commit_ms")),
    )
    reply_phases: dict[str, int] = {}
    remaining = reply_budget
    for key, value in raw_reply_phases:
        reply_phases[key] = min(value, remaining)
        remaining -= reply_phases[key]

    return {
        "timing_total_ms": total,
        **reply_phases,
        "timing_context_ms": context,
        "timing_send_ms": send,
        "timing_other_ms": remaining,
    }


def _nonnegative_metric(metrics: Mapping[str, int | str], key: str) -> int:
    value = metrics.get(key, 0)
    return max(value, 0) if isinstance(value, int) else 0


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
    additive_metrics = build_additive_timing_metrics(metrics)
    profile_metrics = {
        key: value
        for key in CONTEXT_PROFILE_TIMING_PARTS
        if (value := metrics.get(key)) is not None
        and key.endswith("_ms")
        and isinstance(value, int)
    }
    events = [
        MessageDebugEvent(
            part=key,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            latency_ms=max(value, 0),
        )
        for key, value in {**additive_metrics, **profile_metrics}.items()
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


async def prune_agent_telemetry_before(
    session: AsyncSession,
    *,
    cutoff: datetime,
) -> int:
    deleted = 0
    for model in (AgentStepEvent, ToolCallEvent, AgentRunEvent):
        result = await session.execute(delete(model).where(model.created_at < cutoff))
        deleted += int(result.rowcount or 0)
    return deleted


async def prune_action_idempotency_before(
    session: AsyncSession,
    *,
    cutoff: datetime,
) -> int:
    result = await session.execute(
        delete(AppState).where(
            AppState.key.like("send_once:%"),
            AppState.updated_at < cutoff,
        )
    )
    return int(result.rowcount or 0)
