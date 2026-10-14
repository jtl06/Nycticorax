"""Short-lived quote data and single-flight fetches, scoped to one Yahoo client."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
import time


QUOTE_CACHE_TTL_SECONDS = 15.0
QUOTE_CACHE_CAPACITY = 128


class QuotePageCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: OrderedDict[str, tuple[float, Mapping[str, object]]] = OrderedDict()
        self._pending: dict[str, asyncio.Task[Mapping[str, object]]] = {}

    async def get(
        self, symbol: str, load: Callable[[], Awaitable[Mapping[str, object]]],
    ) -> Mapping[str, object]:
        while True:
            now = self._clock()
            for key, (expires, _) in list(self._values.items()):
                if expires <= now:
                    del self._values[key]
            cached = self._values.get(symbol)
            if cached is not None:
                self._values.move_to_end(symbol)
                return cached[1]
            task = self._pending.get(symbol)
            if task is not None:
                return await asyncio.shield(task)
            if len(self._pending) < QUOTE_CACHE_CAPACITY:
                task = asyncio.create_task(self._load(symbol, load))
                self._pending[symbol] = task
                task.add_done_callback(_observe_failure)
                return await asyncio.shield(task)
            # Bound distinct in-flight symbols without cancelling shared fetches.
            await asyncio.wait(tuple(self._pending.values()), return_when=asyncio.FIRST_COMPLETED)

    async def _load(
        self, symbol: str, load: Callable[[], Awaitable[Mapping[str, object]]],
    ) -> Mapping[str, object]:
        expires = self._clock() + QUOTE_CACHE_TTL_SECONDS
        try:
            value = await load()
            if self._clock() < expires:
                self._values[symbol] = (expires, value)
                self._values.move_to_end(symbol)
                while len(self._values) > QUOTE_CACHE_CAPACITY:
                    self._values.popitem(last=False)
            return value
        finally:
            self._pending.pop(symbol, None)


def _observe_failure(task: asyncio.Task) -> None:
    # All waiters can leave before the bounded HTTP fetch finishes.
    if not task.cancelled():
        task.exception()
