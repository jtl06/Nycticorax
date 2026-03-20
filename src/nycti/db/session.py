from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import Base
from nycti.timezones import DEFAULT_TIMEZONE_NAME


def _normalize_database_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(_normalize_database_url(settings.database_url), future=True)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def init_models(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await self._run_lightweight_migrations(connection)

    @asynccontextmanager
    async def session(self) -> AsyncSession:
        async with self.session_factory() as session:
            yield session

    async def _run_lightweight_migrations(self, connection) -> None:
        needs_timezone_column = await connection.run_sync(self._user_settings_missing_timezone_column)
        if needs_timezone_column:
            await connection.execute(
                text(
                    "ALTER TABLE user_settings "
                    f"ADD COLUMN timezone_name VARCHAR(64) NOT NULL DEFAULT '{DEFAULT_TIMEZONE_NAME}'"
                )
            )

    @staticmethod
    def _user_settings_missing_timezone_column(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        if "user_settings" not in tables:
            return False
        columns = {column["name"] for column in inspector.get_columns("user_settings")}
        return "timezone_name" not in columns
