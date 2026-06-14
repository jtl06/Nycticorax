from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from nycti.timing import elapsed_ms

if TYPE_CHECKING:
    from nycti.agent_trace import AgentTrace
    from nycti.chat.run_state import AgentRun
    from nycti.db.session import Database

LOGGER = logging.getLogger(__name__)


class AgentRunTelemetryWriter:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def flush(
        self,
        run: AgentRun,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> tuple[int, int]:
        if not (run.step_records or run.usage_records) or not hasattr(self.database, "session"):
            return 0, 0
        try:
            from nycti.db.models import AgentStepEvent, ToolCallEvent, UsageEvent

            step_events = [
                AgentStepEvent(
                    run_id=run.run_id,
                    step_index=record.step_index,
                    state=str(record.state),
                    feature=record.feature or None,
                    requested_model=record.requested_model or None,
                    active_model=record.active_model or None,
                    provider=record.provider or None,
                    attempt=record.attempt,
                    tool_name=record.tool_name or None,
                    argument_hash=record.argument_hash or None,
                    status=record.status or None,
                    stop_reason=record.stop_reason or None,
                    prompt_version=record.prompt_version,
                    latency_ms=max(record.latency_ms, 0),
                    prompt_tokens=max(record.prompt_tokens, 0),
                    completion_tokens=max(record.completion_tokens, 0),
                    total_tokens=max(record.total_tokens, 0),
                    details=record.details,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                for record in run.step_records
            ]
            usage_events = [
                UsageEvent(
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
                for usage in run.usage_records
            ]
            tool_events = [
                ToolCallEvent(
                    tool_name=record.tool_name,
                    status=record.status or "ok",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    latency_ms=max(record.latency_ms, 0),
                )
                for record in run.step_records
                if record.tool_name
            ]
            write_started_at = time.perf_counter()
            async with self.database.session() as session:
                session.add_all([*step_events, *usage_events, *tool_events])
                write_ms = elapsed_ms(write_started_at)
                commit_started_at = time.perf_counter()
                await session.commit()
                commit_ms = elapsed_ms(commit_started_at)
            return write_ms, commit_ms
        except Exception:  # pragma: no cover - defensive telemetry path
            LOGGER.exception("Agent run telemetry flush failed for run %s.", run.run_id)
            return 0, 0


async def complete_agent_run(
    *,
    writer: AgentRunTelemetryWriter | None,
    run: AgentRun,
    text: str,
    reasoning: list[str],
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int,
) -> tuple[str, list[str]]:
    from nycti.chat.loop_messages import finish_run
    from nycti.chat.run_state import AgentStep, StopReason

    result = finish_run(run, text, reasoning, metrics, trace)
    run.add_step_record(
        state=AgentStep.DONE,
        status="stopped",
        stop_reason=str(run.stop_reason or StopReason.FINAL_TEXT),
    )
    if writer is not None:
        flush_result = await writer.flush(
            run,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        if metrics is not None and flush_result is not None:
            write_ms, commit_ms = flush_result
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + write_ms
            metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
    return result
