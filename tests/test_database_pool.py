import unittest
from unittest.mock import Mock

from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool, QueuePool, StaticPool

from nycti.config import ConfigurationError, Settings
from nycti.db.session import Database


def settings(**overrides):
    return Settings(discord_token="test", openai_api_key="test",
                    database_url=overrides.pop("database_url", "postgresql+psycopg://test:test@localhost/test"),
                    **overrides)


class DatabasePoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_pool_defaults_and_overrides_without_connecting(self):
        for size, overflow in ((2, 13), (3, 4), (1, 0)):
            with self.subTest(size=size, overflow=overflow):
                database = Database(settings(database_pool_size=size, database_max_overflow=overflow))
                try:
                    self.assertIsInstance(database.engine.pool, AsyncAdaptedQueuePool)
                    self.assertEqual(size, database.engine.pool.size())
                    self.assertEqual(overflow, database.engine.pool._max_overflow)
                    self.assertEqual(0, database.engine.pool.checkedout())
                finally:
                    await database.engine.dispose()

    async def test_sqlite_in_memory_pool_and_queries_are_unchanged(self):
        database = Database(settings(database_url="sqlite+aiosqlite:///:memory:"))
        try:
            self.assertIsInstance(database.engine.pool, StaticPool)
            async with database.session() as session:
                self.assertEqual(1, await session.scalar(text("SELECT 1")))
        finally:
            await database.engine.dispose()

    def test_burst_capacity_is_preserved_and_extra_connections_close_on_return(self):
        config = settings()
        physical = []

        def create():
            connection = Mock()
            physical.append(connection)
            return connection

        pool = QueuePool(create, pool_size=config.database_pool_size, max_overflow=config.database_max_overflow)
        connections = []
        try:
            connections = [pool.connect() for _ in range(15)]
            self.assertEqual(15, pool.checkedout())
            for connection in connections:
                connection.close()
            self.assertEqual(2, pool.checkedin())
            self.assertEqual(0, pool.checkedout())
            self.assertEqual(13, sum(connection.close.call_count for connection in physical))
        finally:
            for connection in connections:
                connection.close()
            pool.dispose()

    def test_pool_configuration_loads_and_rejects_unbounded_values(self):
        env = {"DISCORD_TOKEN": "test", "OPENAI_API_KEY": "test", "DATABASE_URL": "sqlite:///test.db"}
        defaults = Settings.from_env(env)
        self.assertEqual((2, 13), (defaults.database_pool_size, defaults.database_max_overflow))
        custom = Settings.from_env({**env, "DATABASE_POOL_SIZE": "3", "DATABASE_MAX_OVERFLOW": "4"})
        self.assertEqual((3, 4), (custom.database_pool_size, custom.database_max_overflow))
        for key, value in (("DATABASE_POOL_SIZE", "0"), ("DATABASE_POOL_SIZE", "21"),
                           ("DATABASE_MAX_OVERFLOW", "-1"), ("DATABASE_MAX_OVERFLOW", "31"),
                           ("DATABASE_POOL_SIZE", "invalid")):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError):
                Settings.from_env({**env, key: value})
        for key, value in (("database_pool_size", True), ("database_max_overflow", 1.5)):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError):
                settings(**{key: value})
