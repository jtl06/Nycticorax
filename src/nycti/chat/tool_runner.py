from __future__ import annotations

import asyncio
from collections.abc import Sequence
import re
import time
from typing import Protocol

from nycti.chat.run_state import AgentPermissions, ToolOutcome, ToolStatus
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
    ) -> tuple[str, dict[str, int | str]]: ...


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
            content, metrics = await self.executor.execute(
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

        cleaned = content.strip()
        status = _status_from_content(cleaned)
        spec = get_tool_spec(tool_call.name)
        if status != ToolStatus.OK and spec is not None:
            cleaned = f"{cleaned}\nNext step: {spec.fallback}".strip()
        return ToolOutcome(
            call_id=tool_call.id,
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            status=status,
            content=cleaned,
            metrics=metrics,
            provenance=tuple(dict.fromkeys(URL_RE.findall(cleaned))),
            retryable=status == ToolStatus.ERROR and _looks_retryable(cleaned),
            latency_ms=elapsed_ms(started_at),
        )


def _status_from_content(content: str) -> ToolStatus:
    if not content:
        return ToolStatus.EMPTY
    normalized = content.casefold()
    empty_signals = (
        "no extractable content found",
        "no web results found",
        "found no older messages",
        "returned no usable result",
    )
    if any(signal in normalized for signal in empty_signals):
        return ToolStatus.EMPTY
    error_signals = (
        " failed",
        "failed ",
        " error",
        "unavailable",
        "disabled",
        "unknown tool",
    )
    if any(signal in normalized for signal in error_signals):
        return ToolStatus.ERROR
    return ToolStatus.OK


def _looks_retryable(content: str) -> bool:
    normalized = content.casefold()
    return any(
        signal in normalized
        for signal in (
            "timeout",
            "timed out",
            "request failed",
            "provider",
            "temporarily",
            "rate limit",
            "connection",
        )
    )
