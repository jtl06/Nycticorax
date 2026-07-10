from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json

from sqlalchemy import delete, inspect, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import Base
from nycti.timezones import DEFAULT_TIMEZONE_NAME

LEGACY_FEEDBACK_PREFIX = "feedback_snapshot:"


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
    async def session(self) -> AsyncIterator[AsyncSession]:
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
        needs_memory_visibility_column = await connection.run_sync(
            self._memory_missing_visibility_column
        )
        if needs_memory_visibility_column:
            await connection.execute(
                text(
                    "ALTER TABLE memories ADD COLUMN visibility VARCHAR(24) "
                    "NOT NULL DEFAULT 'private'"
                )
            )
        await self._migrate_legacy_response_diagnostics(connection)

    @staticmethod
    async def _migrate_legacy_response_diagnostics(connection) -> None:
        """Move commit-5924647 feedback JSON into the indexed expiring tables."""

        from nycti.db.models import (
            AppState,
            ResponseDiagnosticMessageRecord,
            ResponseDiagnosticSnapshotRecord,
        )

        legacy_rows = list(
            (
                await connection.execute(
                    select(AppState.key, AppState.value).where(
                        AppState.key.startswith(
                            LEGACY_FEEDBACK_PREFIX,
                            autoescape=True,
                        )
                    )
                )
            ).all()
        )
        if not legacy_rows:
            return

        now = datetime.now(timezone.utc)
        parsed_rows = [
            parsed
            for key, value in legacy_rows
            if (parsed := _parse_legacy_response_diagnostic(key, value, now=now))
            is not None
        ]
        source_ids = [snapshot.source_message_id for snapshot in parsed_rows]
        bot_message_ids = [
            message_id
            for snapshot in parsed_rows
            for message_id in snapshot.bot_message_ids
        ]
        existing_source_ids = (
            set(
                await connection.scalars(
                    select(ResponseDiagnosticSnapshotRecord.source_message_id).where(
                        ResponseDiagnosticSnapshotRecord.source_message_id.in_(source_ids)
                    )
                )
            )
            if source_ids
            else set()
        )
        existing_message_sources = (
            {
                int(message_id): int(source_id)
                for message_id, source_id in (
                    await connection.execute(
                        select(
                            ResponseDiagnosticMessageRecord.bot_message_id,
                            ResponseDiagnosticMessageRecord.source_message_id,
                        ).where(
                            ResponseDiagnosticMessageRecord.bot_message_id.in_(
                                bot_message_ids
                            )
                        )
                    )
                ).all()
            }
            if bot_message_ids
            else {}
        )

        for snapshot in parsed_rows:
            source_message_id = snapshot.source_message_id
            if any(
                existing_message_sources.get(message_id, source_message_id)
                != source_message_id
                for message_id in snapshot.bot_message_ids
            ):
                continue
            if source_message_id not in existing_source_ids:
                await connection.execute(
                    insert(ResponseDiagnosticSnapshotRecord).values(
                        source_message_id=source_message_id,
                        **_legacy_snapshot_record_values(snapshot),
                    )
                )
                existing_source_ids.add(source_message_id)
            for message_id in snapshot.bot_message_ids:
                if message_id in existing_message_sources:
                    continue
                await connection.execute(
                    insert(ResponseDiagnosticMessageRecord).values(
                        bot_message_id=message_id,
                        source_message_id=source_message_id,
                    )
                )
                existing_message_sources[message_id] = source_message_id

        # Every matching legacy row is now either migrated, already represented,
        # expired, or malformed. Exact primary-key deletion avoids LIKE wildcard
        # behavior for the underscores in the reserved prefix.
        await connection.execute(
            delete(AppState).where(
                AppState.key.in_([key for key, _ in legacy_rows])
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

    @staticmethod
    def _memory_missing_visibility_column(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        if "memories" not in tables:
            return False
        columns = {column["name"] for column in inspector.get_columns("memories")}
        return "visibility" not in columns


def _parse_legacy_response_diagnostic(
    key: str,
    value: str,
    *,
    now: datetime,
):
    from nycti.feedback import FEEDBACK_MAX_AGE, ResponseDiagnosticSnapshot

    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            return None
        captured_at = datetime.fromisoformat(_required_string(payload, "captured_at"))
        captured_at = _as_utc(captured_at)
        if captured_at + FEEDBACK_MAX_AGE <= now:
            return None
        source_message_id = _positive_int(payload["source_message_id"])
        if key != f"{LEGACY_FEEDBACK_PREFIX}{source_message_id}":
            return None
        bot_message_ids = tuple(
            dict.fromkeys(
                _positive_int(message_id)
                for message_id in _required_list(payload, "bot_message_ids")
            )
        )
        if not bot_message_ids:
            return None
        metrics = payload.get("metrics", {})
        if not isinstance(metrics, dict):
            return None
        return ResponseDiagnosticSnapshot(
            captured_at=captured_at,
            guild_id=_positive_int(payload["guild_id"]),
            channel_id=_positive_int(payload["channel_id"]),
            source_message_id=source_message_id,
            source_message_url=_required_string(payload, "source_message_url"),
            source_user_id=_positive_int(payload["source_user_id"]),
            prompt=_required_string(payload, "prompt"),
            context_lines=tuple(
                str(item) for item in _required_list(payload, "context_lines")
            ),
            image_context_lines=tuple(
                str(item)
                for item in _required_list(payload, "image_context_lines")
            ),
            reply_text=_required_string(payload, "reply_text"),
            metrics={str(metric_key): item for metric_key, item in metrics.items()},
            bot_message_ids=bot_message_ids,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _legacy_snapshot_record_values(snapshot) -> dict[str, object]:
    # Reapply current redaction while converting a legacy payload so a manually
    # modified AppState row cannot bypass the structured store's privacy boundary.
    from nycti.feedback import _redacted_snapshot_payload

    return _redacted_snapshot_payload(snapshot)


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _required_list(payload: dict[str, object], key: str) -> list[object]:
    value = payload[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("Discord IDs must be integers")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("Discord IDs must be positive")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
