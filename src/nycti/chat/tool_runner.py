from __future__ import annotations

import asyncio
from collections.abc import Sequence
import re
import time
from typing import Protocol

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolOutcome, ToolStatus
from nycti.chat.tools.registry import get_tool_spec
from nycti.timing import elapsed_ms

URL_RE = re.compile(r"https?://[^\s<>\])]+", re.IGNORECASE)


class ToolCallLike(Protocol):
    id: str
    name: str
    arguments: str


class ToolExecutorLike(Protocol):
    async def execute(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions,
        run_id: str,
        step_index: int,
    ) -> ToolExecutionResult: ...


class ToolRunner:
    def __init__(self, executor: ToolExecutorLike) -> None:
        self.executor = executor

    async def run(
        self,
        tool_calls: Sequence[ToolCallLike],
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions,
        run_id: str,
        step_index: int,
    ) -> list[ToolOutcome]:
        return list(
            await asyncio.gather(
                *[
                    self._run_one(
                        tool_call,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        source_message_id=source_message_id,
                        permissions=permissions,
                        run_id=run_id,
                        step_index=step_index,
                    )
                    for tool_call in tool_calls
                ]
            )
        )

    async def _run_one(
        self,
        tool_call: ToolCallLike,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions,
        run_id: str,
        step_index: int,
    ) -> ToolOutcome:
        started_at = time.perf_counter()
        try:
            execution = await self.executor.execute(
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                permissions=permissions,
                run_id=run_id,
                step_index=step_index,
            )
        except Exception as exc:
            return ToolOutcome(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                status=ToolStatus.ERROR,
                content=f"{tool_call.name} failed: {type(exc).__name__}: {exc}",
                retryable=True,
                latency_ms=elapsed_ms(started_at),
            )

        cleaned = execution.content.strip()
        status = execution.status
        spec = get_tool_spec(tool_call.name)
        if status != ToolStatus.OK and spec is not None:
            cleaned = f"{cleaned}\nNext step: {spec.fallback}".strip()
        return ToolOutcome(
            call_id=tool_call.id,
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            status=status,
            content=cleaned,
            metrics=execution.metrics,
            provenance=execution.provenance or tuple(dict.fromkeys(URL_RE.findall(cleaned))),
            retryable=execution.retryable,
            latency_ms=elapsed_ms(started_at),
        )
