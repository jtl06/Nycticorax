from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class ResponseProgressPhase(StrEnum):
    CONTEXT = "context"
    MODEL = "model"
    TOOLS = "tools"
    COMPOSING = "composing"
    DELIVERING = "delivering"


class ResponseProgressReporter(Protocol):
    async def advance(self, phase: ResponseProgressPhase) -> None: ...


async def advance_response_progress(
    reporter: ResponseProgressReporter | None,
    phase: ResponseProgressPhase,
) -> None:
    if reporter is not None:
        await reporter.advance(phase)
