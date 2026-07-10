from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.db.models import Base
from nycti.feedback import (
    ResponseDiagnosticCache,
    ResponseDiagnosticSnapshot,
    build_bad_bot_feedback_bundle,
    is_bad_bot_feedback,
    load_persisted_response_diagnostic_snapshot,
    persist_response_diagnostic_snapshot,
    redact_diagnostic_secrets,
)


def _snapshot(*, captured_at: datetime, channel_id: int = 2) -> ResponseDiagnosticSnapshot:
    return ResponseDiagnosticSnapshot(
        captured_at=captured_at,
        guild_id=1,
        channel_id=channel_id,
        source_message_id=3,
        source_message_url="https://discord.com/channels/1/2/3",
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

            self.assertTrue(await persist_response_diagnostic_snapshot(database, snapshot=snapshot))
            loaded = await load_persisted_response_diagnostic_snapshot(
                database,
                guild_id=1,
                channel_id=2,
                reference_message_id=10,
                now=now,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(snapshot.source_message_id, loaded.source_message_id)
            self.assertEqual((10,), loaded.bot_message_ids)
            self.assertNotIn("should-not-leak", loaded.reply_text)
        finally:
            await database.close()

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

    def test_secret_redaction_handles_bearer_and_assignments(self) -> None:
        rendered = redact_diagnostic_secrets(
            "Authorization: Bearer abc.def token=my-token password: hunter2"
        )

        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("my-token", rendered)
        self.assertNotIn("hunter2", rendered)


if __name__ == "__main__":
    unittest.main()


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

    async def close(self) -> None:
        await self.engine.dispose()
