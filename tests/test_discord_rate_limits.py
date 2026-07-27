from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nycti.discord.rate_limits import DiscordRateLimitCircuitBreaker, try_discord_request


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _DiscordError(Exception):
    def __init__(
        self,
        *,
        status: int,
        retry_after: str | None = None,
    ) -> None:
        self.status = status
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(headers=headers)


class DiscordRateLimitCircuitBreakerTests(unittest.TestCase):
    def test_429_opens_process_cooldown_until_clock_advances(self) -> None:
        clock = _Clock()
        breaker = DiscordRateLimitCircuitBreaker(
            default_cooldown_seconds=60,
            clock=clock,
        )
        error = _DiscordError(status=429)

        self.assertTrue(breaker.record_exception(error))
        self.assertTrue(breaker.is_open)
        self.assertEqual(60, breaker.remaining_seconds)

        clock.now += 59.9
        self.assertTrue(breaker.is_open)
        clock.now += 0.1
        self.assertFalse(breaker.is_open)

    def test_authoritative_retry_after_is_not_limited_by_fallback_cap(self) -> None:
        clock = _Clock()
        breaker = DiscordRateLimitCircuitBreaker(
            default_cooldown_seconds=10,
            max_cooldown_seconds=120,
            clock=clock,
        )
        error = _DiscordError(status=429, retry_after="600")

        breaker.record_exception(error)

        self.assertEqual(600, breaker.remaining_seconds)

    def test_fallback_cooldown_still_honors_configured_cap(self) -> None:
        clock = _Clock()
        breaker = DiscordRateLimitCircuitBreaker(
            default_cooldown_seconds=300,
            max_cooldown_seconds=120,
            clock=clock,
        )

        breaker.record_exception(_DiscordError(status=429))

        self.assertEqual(120, breaker.remaining_seconds)

    def test_non_429_does_not_open_circuit(self) -> None:
        breaker = DiscordRateLimitCircuitBreaker()

        self.assertFalse(breaker.record_exception(_DiscordError(status=403)))
        self.assertFalse(breaker.is_open)

    def test_request_helper_does_not_attempt_again_after_first_429(self) -> None:
        breaker = DiscordRateLimitCircuitBreaker(default_cooldown_seconds=60)
        request = AsyncMock(side_effect=_DiscordError(status=429))

        first = asyncio.run(
            try_discord_request(request, circuit_breaker=breaker)
        )
        second = asyncio.run(
            try_discord_request(request, circuit_breaker=breaker)
        )

        self.assertFalse(first)
        self.assertFalse(second)
        request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
