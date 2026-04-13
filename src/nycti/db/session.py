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
        needs_profile_column = await connection.run_sync(self._user_settings_missing_profile_column)
        if needs_profile_column:
            await connection.execute(
                text("ALTER TABLE user_settings ADD COLUMN personal_profile_md TEXT NOT NULL DEFAULT ''")
            )
        needs_memory_embedding_columns = await connection.run_sync(self._memory_missing_embedding_columns)
        if needs_memory_embedding_columns["embedding"]:
            await connection.execute(text("ALTER TABLE memories ADD COLUMN embedding JSON"))
        if needs_memory_embedding_columns["embedding_model"]:
            await connection.execute(text("ALTER TABLE memories ADD COLUMN embedding_model VARCHAR(255)"))

    @staticmethod
    def _user_settings_missing_timezone_column(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        if "user_settings" not in tables:
            return False
        columns = {column["name"] for column in inspector.get_columns("user_settings")}
        return "timezone_name" not in columns

    @staticmethod
    def _user_settings_missing_profile_column(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        if "user_settings" not in tables:
            return False
        columns = {column["name"] for column in inspector.get_columns("user_settings")}
        return "personal_profile_md" not in columns

    @staticmethod
    def _memory_missing_embedding_columns(sync_connection) -> dict[str, bool]:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        if "memories" not in tables:
            return {"embedding": False, "embedding_model": False}
        columns = {column["name"] for column in inspector.get_columns("memories")}
        return {
            "embedding": "embedding" not in columns,
            "embedding_model": "embedding_model" not in columns,
        }
