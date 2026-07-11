from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import (
    BadBotFeedbackRecord,
    Base,
    ResponseDiagnosticMessageRecord,
    ResponseDiagnosticSnapshotRecord,
)
from nycti.db.session import Database
from nycti.feedback import (
    ResponseDiagnosticCache,
    ResponseDiagnosticSnapshot,
    archive_bad_bot_feedback,
    build_bad_bot_feedback_bundle,
    is_bad_bot_feedback,
    load_persisted_response_diagnostic_snapshot,
    persist_response_diagnostic_snapshot,
    redact_diagnostic_secrets,
)


def _snapshot(
    *,
    captured_at: datetime,
    guild_id: int = 1,
    channel_id: int = 2,
    source_message_id: int = 3,
) -> ResponseDiagnosticSnapshot:
    return ResponseDiagnosticSnapshot(
        captured_at=captured_at,
        guild_id=guild_id,
        channel_id=channel_id,
        source_message_id=source_message_id,
        source_message_url=(
            f"https://discord.com/channels/{guild_id}/{channel_id}/{source_message_id}"
        ),
        source_user_id=4,
        prompt="why is the market down?",
        context_lines=("user: earlier context",),
        image_context_lines=(),
        reply_text="Because of token: should-not-leak",
        metrics={
            "agent_run_id": "run-123",
            "chat_total_tokens": 120,
            "_diagnostic_agent_messages_json": '[{"role":"tool","content":"evidence"}]',
            "_diagnostic_tool_schemas_json": '[{"name":"web"}]',
        },
    )


def _legacy_snapshot_payload(
    snapshot: ResponseDiagnosticSnapshot,
    *,
    bot_message_ids: tuple[int, ...],
) -> str:
    return json.dumps(
        {
            "captured_at": snapshot.captured_at.isoformat(),
            "guild_id": snapshot.guild_id,
            "channel_id": snapshot.channel_id,
            "source_message_id": snapshot.source_message_id,
            "source_message_url": snapshot.source_message_url,
            "source_user_id": snapshot.source_user_id,
            "prompt": snapshot.prompt,
            "context_lines": list(snapshot.context_lines),
            "image_context_lines": list(snapshot.image_context_lines),
            "reply_text": snapshot.reply_text,
            "metrics": snapshot.metrics,
            "bot_message_ids": list(bot_message_ids),
        }
    )


class BadBotFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def test_feedback_phrase_is_anchored_and_allows_detail(self) -> None:
        self.assertTrue(is_bad_bot_feedback("bad bot"))
        self.assertTrue(is_bad_bot_feedback("Bad bot: that price is stale"))
        self.assertFalse(is_bad_bot_feedback("is this a bad bot benchmark?"))

    def test_cache_matches_reply_or_latest_recent_response(self) -> None:
        now = datetime.now(timezone.utc)
        cache = ResponseDiagnosticCache(max_entries=2, max_age=timedelta(minutes=5))
        snapshot = _snapshot(captured_at=now)
        cache.record(snapshot, bot_message_ids=[10, 11])

        self.assertIs(
            snapshot,
            cache.find(channel_id=2, reference_message_id=11, now=now),
        )
        self.assertIs(
            snapshot,
            cache.find(channel_id=2, reference_message_id=None, now=now),
        )
        self.assertIsNone(
            cache.find(channel_id=9, reference_message_id=None, now=now),
        )

    def test_cache_expires_old_responses(self) -> None:
        now = datetime.now(timezone.utc)
        cache = ResponseDiagnosticCache(max_age=timedelta(minutes=5))
        cache.record(
            _snapshot(captured_at=now - timedelta(minutes=6)),
            bot_message_ids=[10],
        )

        self.assertIsNone(cache.find(channel_id=2, reference_message_id=10, now=now))

    async def test_persisted_snapshot_survives_cache_loss_and_is_redacted(self) -> None:
        database = await _FeedbackDatabase.create()
        try:
            now = datetime.now(timezone.utc)
            snapshot = _snapshot(captured_at=now)
            snapshot.bot_message_ids = (10,)

            self.assertFalse(
                await persist_response_diagnostic_snapshot(
                    database,
                    snapshot=snapshot,
                )
            )
            self.assertEqual(0, await database.count(ResponseDiagnosticSnapshotRecord))
            self.assertTrue(
                await persist_response_diagnostic_snapshot(
                    database,
                    snapshot=snapshot,
                    enabled=True,
                )
            )
            self.assertIsNone(
                await load_persisted_response_diagnostic_snapshot(
                    database,
                    guild_id=1,
                    channel_id=2,
                    reference_message_id=10,
                    now=now,
                )
            )
            loaded = await load_persisted_response_diagnostic_snapshot(
                database,
                guild_id=1,
                channel_id=2,
                reference_message_id=10,
                now=now,
                enabled=True,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(snapshot.source_message_id, loaded.source_message_id)
            self.assertEqual((10,), loaded.bot_message_ids)
            self.assertNotIn("should-not-leak", loaded.reply_text)
        finally:
            await database.close()

    async def test_persisted_lookup_filters_scope_before_any_result_limit(self) -> None:
        database = await _FeedbackDatabase.create()
        try:
            now = datetime.now(timezone.utc)
            target = _snapshot(captured_at=now, source_message_id=3)
            target.bot_message_ids = (10, 11)
            self.assertTrue(
                await persist_response_diagnostic_snapshot(
                    database,
                    snapshot=target,
                    enabled=True,
                )
            )
            for offset in range(25):
                other = _snapshot(
                    captured_at=now + timedelta(microseconds=offset + 1),
                    channel_id=99,
                    source_message_id=100 + offset,
                )
                other.bot_message_ids = (1000 + offset,)
                self.assertTrue(
                    await persist_response_diagnostic_snapshot(
                        database,
                        snapshot=other,
                        enabled=True,
                    )
                )

            loaded = await load_persisted_response_diagnostic_snapshot(
                database,
                guild_id=1,
                channel_id=2,
                reference_message_id=11,
                now=now + timedelta(seconds=1),
                enabled=True,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(3, loaded.source_message_id)
            self.assertEqual((10, 11), loaded.bot_message_ids)
            self.assertIsNone(
                await load_persisted_response_diagnostic_snapshot(
                    database,
                    guild_id=1,
                    channel_id=99,
                    reference_message_id=11,
                    now=now + timedelta(seconds=1),
                    enabled=True,
                )
            )
        finally:
            await database.close()

    async def test_expired_rows_are_pruned_on_read_and_write(self) -> None:
        database = await _FeedbackDatabase.create()
        try:
            now = datetime.now(timezone.utc)
            old = _snapshot(captured_at=now, source_message_id=3)
            old.bot_message_ids = (10,)
            await persist_response_diagnostic_snapshot(
                database,
                snapshot=old,
                enabled=True,
            )

            later = now + timedelta(minutes=16)
            replacement = _snapshot(captured_at=later, source_message_id=4)
            replacement.bot_message_ids = (20,)
            await persist_response_diagnostic_snapshot(
                database,
                snapshot=replacement,
                enabled=True,
            )
            self.assertEqual(1, await database.count(ResponseDiagnosticSnapshotRecord))
            self.assertEqual(1, await database.count(ResponseDiagnosticMessageRecord))

            self.assertIsNone(
                await load_persisted_response_diagnostic_snapshot(
                    database,
                    guild_id=1,
                    channel_id=2,
                    reference_message_id=20,
                    now=later + timedelta(minutes=16),
                    enabled=True,
                )
            )
            self.assertEqual(0, await database.count(ResponseDiagnosticSnapshotRecord))
            self.assertEqual(0, await database.count(ResponseDiagnosticMessageRecord))
        finally:
            await database.close()

    async def test_startup_migrates_to_indexed_structured_feedback_tables(self) -> None:
        settings = Settings(
            discord_token="discord-token",
            openai_api_key="openai-key",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        database = Database(settings)
        try:
            now = datetime.now(timezone.utc)
            valid = _snapshot(captured_at=now, source_message_id=3)
            expired = _snapshot(
                captured_at=now - timedelta(minutes=16),
                source_message_id=5,
            )
            async with database.engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE app_state ("
                        "key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL, "
                        "updated_at DATETIME NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO app_state (key, value, updated_at) "
                        "VALUES (:key, :value, :updated_at)"
                    ),
                    [
                        {
                            "key": "feedback_snapshot:3",
                            "value": _legacy_snapshot_payload(
                                valid,
                                bot_message_ids=(10, 11),
                            ),
                            "updated_at": now,
                        },
                        {
                            "key": "feedback_snapshot:4",
                            "value": "{malformed",
                            "updated_at": now,
                        },
                        {
                            "key": "feedback_snapshot:5",
                            "value": _legacy_snapshot_payload(
                                expired,
                                bot_message_ids=(12,),
                            ),
                            "updated_at": now,
                        },
                        {
                            "key": "feedbackXsnapshot:6",
                            "value": "literal-prefix-near-match",
                            "updated_at": now,
                        },
                        {
                            "key": "unrelated",
                            "value": "keep",
                            "updated_at": now,
                        },
                    ],
                )

            await database.init_models()

            async with database.engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda sync_connection: set(inspect(sync_connection).get_table_names())
                )
                indexes = await connection.run_sync(
                    lambda sync_connection: {
                        item["name"]
                        for item in inspect(sync_connection).get_indexes(
                            "response_diagnostic_snapshots"
                        )
                    }
                )
                exact_legacy_count = await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM app_state "
                        "WHERE key IN ("
                        "'feedback_snapshot:3', "
                        "'feedback_snapshot:4', "
                        "'feedback_snapshot:5'"
                        ")"
                    )
                )
                near_match_count = await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM app_state "
                        "WHERE key = 'feedbackXsnapshot:6'"
                    )
                )
                unrelated_count = await connection.scalar(
                    text("SELECT COUNT(*) FROM app_state WHERE key = 'unrelated'")
                )
                snapshot_count = await connection.scalar(
                    text("SELECT COUNT(*) FROM response_diagnostic_snapshots")
                )
                message_count = await connection.scalar(
                    text("SELECT COUNT(*) FROM response_diagnostic_messages")
                )

            self.assertIn("response_diagnostic_snapshots", tables)
            self.assertIn("response_diagnostic_messages", tables)
            self.assertIn("ix_response_diag_scope_expiry", indexes)
            self.assertEqual(0, exact_legacy_count)
            self.assertEqual(1, near_match_count)
            self.assertEqual(1, unrelated_count)
            self.assertEqual(1, snapshot_count)
            self.assertEqual(2, message_count)

            loaded = await load_persisted_response_diagnostic_snapshot(
                database,
                guild_id=1,
                channel_id=2,
                reference_message_id=11,
                now=now,
                enabled=True,
            )
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(3, loaded.source_message_id)
            self.assertEqual((10, 11), loaded.bot_message_ids)
            self.assertNotIn("should-not-leak", loaded.reply_text)
        finally:
            await database.engine.dispose()

    async def test_bundle_contains_replay_context_and_redacts_credentials(self) -> None:
        snapshot = _snapshot(captured_at=datetime.now(timezone.utc))
        snapshot.metrics["api_key"] = "secret-value"

        bundle = await build_bad_bot_feedback_bundle(
            SimpleNamespace(),
            snapshot=snapshot,
            feedback_message_id=5,
            feedback_message_url="https://discord.com/channels/1/2/5",
            feedback_user_id=6,
            feedback_text="bad bot: wrong catalyst",
        )

        self.assertIn("why is the market down?", bundle)
        self.assertIn("user: earlier context", bundle)
        self.assertIn("agent_messages_and_tool_results", bundle)
        self.assertIn('"content":"evidence"', bundle)
        self.assertIn("bad bot: wrong catalyst", bundle)
        self.assertNotIn("secret-value", bundle)
        self.assertNotIn("should-not-leak", bundle)

    async def test_explicit_feedback_is_archived_after_snapshot_expiry(self) -> None:
        database = await _FeedbackDatabase.create()
        try:
            snapshot = _snapshot(captured_at=datetime.now(timezone.utc))
            feedback_message = SimpleNamespace(
                id=5,
                jump_url="https://discord.com/channels/1/2/5",
                author=SimpleNamespace(id=6),
                content="bad bot: wrong catalyst",
            )
            bundle = await build_bad_bot_feedback_bundle(
                database,
                snapshot=snapshot,
                feedback_message_id=feedback_message.id,
                feedback_message_url=feedback_message.jump_url,
                feedback_user_id=feedback_message.author.id,
                feedback_text=feedback_message.content,
            )

            self.assertTrue(
                await archive_bad_bot_feedback(
                    database,
                    snapshot=snapshot,
                    feedback_message=feedback_message,
                    bundle=bundle,
                )
            )
            self.assertEqual(1, await database.count(BadBotFeedbackRecord))
            async with database.session() as session:
                record = await session.get(BadBotFeedbackRecord, feedback_message.id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(snapshot.source_message_id, record.source_message_id)
            self.assertIn("wrong catalyst", record.bundle)
        finally:
            await database.close()

    def test_secret_redaction_handles_bearer_and_assignments(self) -> None:
        rendered = redact_diagnostic_secrets(
            "Authorization: Bearer abc.def token=my-token password: hunter2"
        )

        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("my-token", rendered)
        self.assertNotIn("hunter2", rendered)


class _FeedbackDatabase:
    def __init__(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    async def create(cls) -> "_FeedbackDatabase":
        database = cls()
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return database

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as session:
            yield session

    async def count(self, model) -> int:
        async with self.session() as session:
            return int(
                await session.scalar(select(func.count()).select_from(model))
                or 0
            )

    async def close(self) -> None:
        await self.engine.dispose()


if __name__ == "__main__":
    unittest.main()
