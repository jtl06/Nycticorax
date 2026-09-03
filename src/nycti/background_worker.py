from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Generic, TypeVar


JobT = TypeVar("JobT")


class BoundedBackgroundWorker(Generic[JobT]):
    """Reusable single-consumer queue with bounded, nonblocking submission."""

    def __init__(
        self,
        *,
        handler: Callable[[JobT], Awaitable[None]],
        name: str,
        maxsize: int,
        logger: logging.Logger,
        drain_timeout_seconds: float | None = None,
        error_label: str = "Background job",
    ) -> None:
        self.handler = handler
        self.name = name
        self.logger = logger
        self.drain_timeout_seconds = drain_timeout_seconds
        self.error_label = error_label
        self.queue: asyncio.Queue[JobT] = asyncio.Queue(maxsize=max(1, maxsize))
        self.task: asyncio.Task[None] | None = None
        self.closed = False

    @property
    def pending_count(self) -> int:
        return self.queue.qsize()

    def start(self) -> bool:
        if self.closed:
            return False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=self.name)
        return True

    def submit(self, job: JobT) -> bool:
        if not self.start():
            return False
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            return False
        return True

    async def join(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        self.closed = True
        task = self.task
        if task is None:
            return
        if self.drain_timeout_seconds is not None:
            try:
                await asyncio.wait_for(
                    self.queue.join(),
                    timeout=self.drain_timeout_seconds,
                )
            except TimeoutError:
                self.logger.warning("Timed out draining %s.", self.name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.task = None
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.handler(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive background boundary
                self.logger.exception("%s failed.", self.error_label)
            finally:
                self.queue.task_done()
