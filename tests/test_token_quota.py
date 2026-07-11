from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.db.models import (
    Base,
    DailyModelTokenCounter,
    ModelTokenReservation,
)
from nycti.llm.token_quota import DailyTokenQuota, estimate_reservation_tokens

TERRA = "gpt-5.6-terra"
LUNA = "gpt-5.6-luna"


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _QuotaDatabase:
    def __init__(self, url: str = "sqlite+aiosqlite:///:memory:") -> None:
        self.engine = create_async_engine(url)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


class DailyTokenQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = _QuotaDatabase()
        await self.database.initialize()
        self.clock = _Clock(datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc))

    async def asyncTearDown(self) -> None:
        await self.database.close()

    def quota(self, limit: int = 1_000_000) -> DailyTokenQuota:
        return DailyTokenQuota(
            self.database,
            {TERRA: limit},
            LUNA,
            fallback_reasoning_effort="high",
            now=self.clock,
        )

    async def test_settle_replaces_reservation_with_reported_usage(self) -> None:
        quota = self.quota()
        route = await quota.route(
            TERRA,
            messages=[{"role": "user", "content": "Give me a concise answer."}],
            tools=[{"type": "function", "name": "web"}],
            max_tokens=512,
        )

        self.assertEqual(TERRA, route.model)
        self.assertIsNotNone(route.reservation_id)
        self.assertGreater(route.reserved_tokens, 512)
        self.assertTrue(await quota.settle(route, total_tokens=137))
        self.assertTrue(await quota.settle(route, total_tokens=999))

        async with self.database.session_factory() as session:
            counter = await session.get(
                DailyModelTokenCounter,
                {
                    "provider": "openai",
                    "model": TERRA,
                    "usage_day": self.clock.value.date(),
                },
            )
            reservation = await session.get(
                ModelTokenReservation,
                route.reservation_id,
            )
        assert counter is not None
        assert reservation is not None
        self.assertEqual(137, counter.consumed_tokens)
        self.assertEqual(0, counter.reserved_tokens)
        self.assertEqual("settled", reservation.status)
        self.assertEqual(137, reservation.actual_tokens)

    async def test_capacity_routes_to_high_reasoning_fallback(self) -> None:
        reservation_size = estimate_reservation_tokens([], [], 1)
        quota = self.quota(limit=reservation_size)

        first = await quota.route(TERRA, max_tokens=1)
        second = await quota.route(TERRA, max_tokens=1)
        unlimited = await quota.route("another-model", max_tokens=50_000)

        self.assertFalse(first.used_fallback)
        self.assertEqual(LUNA, second.model)
        self.assertEqual("high", second.reasoning_effort)
        self.assertEqual("daily_limit_reached", second.fallback_reason)
        self.assertIsNone(second.reservation_id)
        self.assertEqual("another-model", unlimited.model)
        self.assertIsNone(unlimited.reservation_id)

    async def test_release_and_uncertain_charge_have_distinct_accounting(self) -> None:
        quota = self.quota()
        released = await quota.route(TERRA, max_tokens=100)
        self.assertTrue(await quota.release(released))

        uncertain = await quota.route(TERRA, max_tokens=100)
        self.assertTrue(await quota.charge_uncertain(uncertain))

        async with self.database.session_factory() as session:
            counter = await session.get(
                DailyModelTokenCounter,
                {
                    "provider": "openai",
                    "model": TERRA,
                    "usage_day": self.clock.value.date(),
                },
            )
            released_record = await session.get(
                ModelTokenReservation,
                released.reservation_id,
            )
            uncertain_record = await session.get(
                ModelTokenReservation,
                uncertain.reservation_id,
            )
        assert counter is not None
        assert released_record is not None
        assert uncertain_record is not None
        self.assertEqual(uncertain.reserved_tokens, counter.consumed_tokens)
        self.assertEqual(0, counter.reserved_tokens)
        self.assertEqual("released", released_record.status)
        self.assertEqual("uncertain", uncertain_record.status)

    async def test_provider_exhaustion_releases_call_and_resets_next_utc_day(self) -> None:
        quota = self.quota()
        route = await quota.route(TERRA, max_tokens=100)

        self.assertTrue(await quota.mark_exhausted(TERRA, route))
        exhausted = await quota.route(TERRA, max_tokens=100)
        self.assertEqual(LUNA, exhausted.model)
        self.assertEqual("provider_exhausted", exhausted.fallback_reason)

        async with self.database.session_factory() as session:
            counter = await session.get(
                DailyModelTokenCounter,
                {
                    "provider": "openai",
                    "model": TERRA,
                    "usage_day": self.clock.value.date(),
                },
            )
            reservation = await session.get(
                ModelTokenReservation,
                route.reservation_id,
            )
        assert counter is not None
        assert reservation is not None
        self.assertTrue(counter.provider_exhausted)
        self.assertEqual(0, counter.reserved_tokens)
        self.assertEqual("provider_exhausted", reservation.status)

        self.clock.value += timedelta(days=1)
        next_day = await quota.route(TERRA, max_tokens=100)
        self.assertEqual(TERRA, next_day.model)
        self.assertIsNotNone(next_day.reservation_id)

    async def test_database_error_fails_closed_to_fallback(self) -> None:
        class _BrokenSessionFactory:
            def __call__(self):
                raise RuntimeError("database unavailable")

        quota = DailyTokenQuota(
            _BrokenSessionFactory(),
            {TERRA: 1_000_000},
            LUNA,
        )

        route = await quota.route(TERRA, max_tokens=100)

        self.assertEqual(LUNA, route.model)
        self.assertEqual("database_error", route.fallback_reason)
        self.assertIsNone(route.reservation_id)


class DailyTokenQuotaConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_reservation_allows_only_available_capacity(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quota.sqlite3"
            database = _QuotaDatabase(f"sqlite+aiosqlite:///{path}")
            await database.initialize()
            try:
                reservation_size = estimate_reservation_tokens([], [], 1)
                quota = DailyTokenQuota(
                    database,
                    {TERRA: reservation_size},
                    LUNA,
                )

                routes = await asyncio.gather(
                    quota.route(TERRA, max_tokens=1),
                    quota.route(TERRA, max_tokens=1),
                )

                self.assertEqual(1, sum(route.model == TERRA for route in routes))
                self.assertEqual(1, sum(route.model == LUNA for route in routes))
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
