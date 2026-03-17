from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

RequestKey = tuple[int, int]


class ActiveRequestRegistry:
    def __init__(self) -> None:
        self._tasks: dict[RequestKey, asyncio.Task[Any]] = {}

    def has_active(self, key: RequestKey) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def start(self, key: RequestKey, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            raise RuntimeError("An active request already exists for this key.")
        task = asyncio.create_task(coro)
        self._tasks[key] = task
        return task

    def cancel(self, key: RequestKey) -> bool:
        task = self._tasks.get(key)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def cancel_all(self) -> int:
        cancelled = 0
        for task in self._tasks.values():
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        return cancelled

    def clear(self, key: RequestKey, task: asyncio.Task[Any]) -> None:
        current = self._tasks.get(key)
        if current is task:
            self._tasks.pop(key, None)
