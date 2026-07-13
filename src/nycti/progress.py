from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol


class ResponseProgressPhase(StrEnum):
    CONTEXT = "context"
    MODEL = "model"
    TOOLS = "tools"
    COMPOSING = "composing"
    DELIVERING = "delivering"


class ResponseProgressReporter(Protocol):
    async def advance(
        self,
        phase: ResponseProgressPhase,
        *,
        tool_names: Sequence[str] = (),
    ) -> None: ...


async def advance_response_progress(
    reporter: ResponseProgressReporter | None,
    phase: ResponseProgressPhase,
    *,
    tool_names: Sequence[str] = (),
) -> None:
    if reporter is not None:
        await reporter.advance(phase, tool_names=tool_names)
