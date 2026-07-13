from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
import logging
import time
from typing import TYPE_CHECKING

from nycti.progress import ResponseProgressPhase

if TYPE_CHECKING:
    import discord


LOGGER = logging.getLogger(__name__)
DEFAULT_PROGRESS_DELAY_SECONDS = 2.0
DEFAULT_PROGRESS_DEBOUNCE_SECONDS = 0.8
DEFAULT_DISCORD_OPERATION_TIMEOUT_SECONDS = 5.0
DEFAULT_PROGRESS_REFRESH_SECONDS = 5.0
SLOW_PROGRESS_SECONDS = 15
_BAR_WIDTH = 10
_ACTIVITY_WIDTH = 3
_MAX_TOOL_SUMMARY_CHARS = 48


_PHASE_ACTIVITY: dict[ResponseProgressPhase, tuple[int, str]] = {
    ResponseProgressPhase.CONTEXT: (0, "Reading context"),
    ResponseProgressPhase.MODEL: (1, "Thinking"),
    ResponseProgressPhase.TOOLS: (3, "Checking sources"),
    ResponseProgressPhase.COMPOSING: (6, "Writing reply"),
    ResponseProgressPhase.DELIVERING: (7, "Preparing reply"),
}


def render_response_progress(
    phase: ResponseProgressPhase,
    *,
    tool_names: Sequence[str] = (),
    reviewing_results: bool = False,
    elapsed_seconds: int = 0,
) -> str:
    start, label = _PHASE_ACTIVITY[phase]
    if phase == ResponseProgressPhase.MODEL and reviewing_results:
        label = "Reviewing results"
    bar = (
        "░" * start
        + "█" * _ACTIVITY_WIDTH
        + "░" * (_BAR_WIDTH - start - _ACTIVITY_WIDTH)
    )
    details: list[str] = []
    if phase == ResponseProgressPhase.TOOLS and tool_names:
        details.append(_format_tool_summary(tool_names))
    if elapsed_seconds >= SLOW_PROGRESS_SECONDS:
        details.extend((f"{elapsed_seconds}s", "/cancel"))
    suffix = f" · {' · '.join(details)}" if details else ""
    return f"`{bar}` {label}{suffix}"


def _format_tool_summary(tool_names: Sequence[str]) -> str:
    counts = Counter(tool_names)
    parts = [f"{name} x{count}" if count > 1 else name for name, count in counts.items()]
    summary = ", ".join(parts)
    if len(summary) <= _MAX_TOOL_SUMMARY_CHARS:
        return summary
    return f"{len(tool_names)} tool calls"


class DiscordResponseProgress:
    """Own one delayed, editable Discord progress message for a response."""

    def __init__(
        self,
        source_message: discord.Message,
        *,
        delay_seconds: float = DEFAULT_PROGRESS_DELAY_SECONDS,
        debounce_seconds: float = DEFAULT_PROGRESS_DEBOUNCE_SECONDS,
        operation_timeout_seconds: float = DEFAULT_DISCORD_OPERATION_TIMEOUT_SECONDS,
        refresh_seconds: float = DEFAULT_PROGRESS_REFRESH_SECONDS,
    ) -> None:
        self._source_message = source_message
        self._delay_seconds = max(delay_seconds, 0.0)
        self._debounce_seconds = max(debounce_seconds, 0.0)
        self._operation_timeout_seconds = max(operation_timeout_seconds, 0.001)
        self._refresh_seconds = max(refresh_seconds, 0.1)
        self._started_at = time.monotonic()
        self._phase = ResponseProgressPhase.CONTEXT
        self._tool_names: tuple[str, ...] = ()
        self._has_run_tools = False
        self._updated = asyncio.Event()
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[discord.Message | None] | None = None
        self._message: discord.Message | None = None
        self._resolved = False

    @property
    def phase(self) -> ResponseProgressPhase:
        return self._phase

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> DiscordResponseProgress:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self

    async def advance(
        self,
        phase: ResponseProgressPhase,
        *,
        tool_names: Sequence[str] = (),
    ) -> None:
        """Queue an activity update without performing Discord I/O."""
        if self._stopped.is_set():
            return
        normalized_tool_names = tuple(str(name) for name in tool_names if str(name))
        if phase == self._phase and normalized_tool_names == self._tool_names:
            return
        self._phase = phase
        self._tool_names = normalized_tool_names if phase == ResponseProgressPhase.TOOLS else ()
        if phase == ResponseProgressPhase.TOOLS:
            self._has_run_tools = True
        self._updated.set()

    async def claim(self) -> discord.Message | None:
        """Stop progress edits and return the message for final-answer replacement."""
        return await self._stop_worker()

    def mark_resolved(self) -> None:
        """Skip cleanup after the bar was replaced or successfully removed."""
        self._resolved = True

    async def discard(self) -> None:
        """Stop and remove an unclaimed progress message, if one was posted."""
        if self._resolved:
            return
        message = await self._stop_worker()
        if message is None:
            return
        try:
            await asyncio.wait_for(
                message.delete(),
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("Discord progress deletion failed; continuing.", exc_info=True)

    async def _stop_worker(self) -> discord.Message | None:
        self._stopped.set()
        self._updated.set()
        task = self._task
        if task is None:
            return self._message
        if task.done():
            if task.cancelled():
                return self._message
            try:
                return task.result()
            except Exception:
                LOGGER.debug("Discord progress worker failed; continuing.", exc_info=True)
                return self._message
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        except Exception:
            LOGGER.debug("Discord progress worker failed; continuing.", exc_info=True)
            return self._message

    async def _run(self) -> discord.Message | None:
        if await self._stopped_before_delay():
            return None
        if self._stopped.is_set():
            return None

        rendered = self._render()
        try:
            message = await asyncio.wait_for(
                self._source_message.reply(rendered, mention_author=False),
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("Discord progress message failed; continuing without it.", exc_info=True)
            return None

        self._message = message
        last_rendered = rendered
        last_edit_at = time.monotonic()
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._updated.wait(),
                    timeout=self._refresh_seconds,
                )
            except TimeoutError:
                pass
            self._updated.clear()
            if self._stopped.is_set():
                break
            remaining = self._debounce_seconds - (time.monotonic() - last_edit_at)
            if remaining > 0 and await self._stops_within(remaining):
                break
            rendered = self._render()
            if rendered == last_rendered:
                continue
            try:
                await asyncio.wait_for(
                    message.edit(content=rendered),
                    timeout=self._operation_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.debug("Discord progress edit failed; continuing.", exc_info=True)
                continue
            last_rendered = rendered
            last_edit_at = time.monotonic()
        return message

    def _render(self) -> str:
        return render_response_progress(
            self._phase,
            tool_names=self._tool_names,
            reviewing_results=self._has_run_tools,
            elapsed_seconds=int(time.monotonic() - self._started_at),
        )

    async def _stopped_before_delay(self) -> bool:
        return await self._stops_within(self._delay_seconds)

    async def _stops_within(self, timeout_seconds: float) -> bool:
        if self._stopped.is_set():
            return True
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True
