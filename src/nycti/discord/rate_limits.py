from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any


LOGGER = logging.getLogger(__name__)
DEFAULT_DISCORD_GLOBAL_COOLDOWN_SECONDS = 60.0
MAX_DISCORD_GLOBAL_COOLDOWN_SECONDS = 300.0


def is_discord_rate_limit_error(exc: BaseException) -> bool:
    """Return whether Discord surfaced an HTTP 429 after its internal retries."""
    return getattr(exc, "status", None) == 429


class DiscordRateLimitCircuitBreaker:
    """Pause optional and retrying Discord writes after a surfaced HTTP 429."""

    def __init__(
        self,
        *,
        default_cooldown_seconds: float = DEFAULT_DISCORD_GLOBAL_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = MAX_DISCORD_GLOBAL_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._default_cooldown_seconds = max(default_cooldown_seconds, 0.0)
        self._max_cooldown_seconds = max(max_cooldown_seconds, 0.0)
        self._clock = clock
        self._blocked_until = 0.0

    @property
    def remaining_seconds(self) -> float:
        return max(self._blocked_until - self._clock(), 0.0)

    @property
    def is_open(self) -> bool:
        return self.remaining_seconds > 0.0

    def record_exception(self, exc: BaseException) -> bool:
        """Trip the breaker for a 429 and report whether the exception was handled."""
        if not is_discord_rate_limit_error(exc):
            return False
        was_open = self.is_open
        cooldown_seconds = self._cooldown_seconds(exc)
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + cooldown_seconds,
        )
        if not was_open:
            LOGGER.warning(
                "Discord returned HTTP 429; pausing outbound Discord traffic for %.1f seconds.",
                cooldown_seconds,
            )
        return True

    def _cooldown_seconds(self, exc: BaseException) -> float:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return max(self._default_cooldown_seconds, retry_after)
        return min(self._default_cooldown_seconds, self._max_cooldown_seconds)


def _retry_after_seconds(exc: BaseException) -> float | None:
    candidates: list[Any] = [getattr(exc, "retry_after", None)]
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            candidates.append(headers.get("Retry-After"))
        except (AttributeError, TypeError):
            pass
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


DISCORD_OUTBOUND_CIRCUIT_BREAKER = DiscordRateLimitCircuitBreaker()


async def try_discord_request(
    request: Callable[[], Awaitable[Any]],
    *,
    circuit_breaker: DiscordRateLimitCircuitBreaker = DISCORD_OUTBOUND_CIRCUIT_BREAKER,
) -> bool:
    """Run one Discord request unless cooling down, swallowing only surfaced 429s."""
    if circuit_breaker.is_open:
        return False
    try:
        await request()
    except Exception as exc:
        if circuit_breaker.record_exception(exc):
            return False
        raise
    return True
