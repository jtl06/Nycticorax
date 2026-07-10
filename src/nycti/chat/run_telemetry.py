from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import TYPE_CHECKING

from nycti.llm.responses_adapter import RESPONSES_OUTPUT_ITEMS_KEY
from nycti.timing import elapsed_ms

if TYPE_CHECKING:
    from nycti.agent_trace import AgentTrace
    from nycti.chat.run_state import AgentRun
    from nycti.db.session import Database

LOGGER = logging.getLogger(__name__)
TELEMETRY_QUEUE_MAXSIZE = 128
TELEMETRY_DRAIN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _PendingTelemetry:
    run: AgentRun
    guild_id: int | None
    channel_id: int | None
    user_id: int


class AgentRunTelemetryWriter:
    def __init__(self, database: Database, *, queue_maxsize: int = TELEMETRY_QUEUE_MAXSIZE) -> None:
        self.database = database
        self.queue_maxsize = max(queue_maxsize, 1)
        self._queue: asyncio.Queue[_PendingTelemetry | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    def submit(
        self,
        run: AgentRun,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> bool:
        """Queue nonessential telemetry without extending reply latency."""
        if (
            self._closed
            or not (run.step_records or run.usage_records)
            or not hasattr(self.database, "session")
        ):
            return False
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.queue_maxsize)
            self._worker = asyncio.create_task(
                self._run_worker(),
                name="nycti-agent-telemetry-writer",
            )
        try:
            self._queue.put_nowait(
                _PendingTelemetry(
                    run=run,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
            )
        except asyncio.QueueFull:
            LOGGER.warning("Agent telemetry queue is full; dropping run %s.", run.run_id)
            return False
        return True

    async def close(self) -> None:
        """Best-effort bounded drain for shutdown."""
        self._closed = True
        queue = self._queue
        worker = self._worker
        if queue is None or worker is None:
            return
        try:
            await asyncio.wait_for(queue.join(), timeout=TELEMETRY_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            LOGGER.warning("Timed out draining agent telemetry during shutdown.")
            worker.cancel()
        else:
            queue.put_nowait(None)
        try:
            await asyncio.wait_for(worker, timeout=TELEMETRY_DRAIN_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        finally:
            self._worker = None
            self._queue = None

    async def _run_worker(self) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - defensive lifecycle guard
            return
        while True:
            pending = await queue.get()
            try:
                if pending is None:
                    return
                await self.flush(
                    pending.run,
                    guild_id=pending.guild_id,
                    channel_id=pending.channel_id,
                    user_id=pending.user_id,
                )
            finally:
                queue.task_done()

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
            from nycti.db.models import AgentRunEvent, AgentStepEvent, ToolCallEvent, UsageEvent

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
            run_event = AgentRunEvent(
                run_id=run.run_id,
                final_status=run.final_status,
                stop_reason=str(run.stop_reason) if run.stop_reason is not None else None,
                failure_reason=run.final_failure_reason[:255] or None,
                model_turn_count=run.model_turns,
                tool_call_count=run.tool_calls,
                correction_count=run.corrections,
                continuation_count=run.continuations,
                latency_ms=run.elapsed_ms(),
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            write_started_at = time.perf_counter()
            async with self.database.session() as session:
                session.add_all([run_event, *step_events, *usage_events, *tool_events])
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

    if metrics is not None:
        metrics["_diagnostic_agent_messages_json"] = _serialize_diagnostic_messages(
            run.messages
        )
    result = finish_run(run, text, reasoning, metrics, trace)
    run.add_step_record(
        state=AgentStep.DONE,
        status="stopped",
        stop_reason=str(run.stop_reason or StopReason.FINAL_TEXT),
    )
    if writer is not None:
        submit = getattr(writer, "submit", None)
        if callable(submit):
            queued = bool(
                submit(
                    run,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
            )
            if metrics is not None:
                metrics["agent_telemetry_queued"] = int(queued)
        else:
            # Compatibility for lightweight test/custom writers.
            await writer.flush(
                run,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
    return result


def _serialize_diagnostic_messages(messages: list[dict[str, object]]) -> str:
    return json.dumps(
        _sanitize_diagnostic_value(messages),
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
        default=str,
    )


def _sanitize_diagnostic_value(value: object) -> object:
    if isinstance(value, list):
        return [_sanitize_diagnostic_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image_url":
            return {"type": "image_url", "image_url": "[image omitted]"}
        return {
            str(key): (
                "[Responses continuation state omitted]"
                if key == RESPONSES_OUTPUT_ITEMS_KEY
                else _sanitize_diagnostic_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return "[embedded image omitted]"
        if len(value) > 40_000:
            return value[:39_984].rstrip() + "\n[truncated]"
    return value
