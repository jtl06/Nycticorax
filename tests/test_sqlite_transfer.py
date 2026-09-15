import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sqlalchemy import create_engine, insert, select, text

from nycti.db.models import Base, DailyModelTokenCounter, Memory, Reminder, UserSettings
from nycti.db.session import Database
from nycti.db.sqlite_transfer import backup_sqlite, copy_to_sqlite
from nycti.config import Settings
from nycti.llm.token_quota import DailyTokenQuota, estimate_reservation_tokens
from nycti.reminders.service import ReminderService
from nycti.memory.retriever import MemoryRetriever


class SQLiteTransferTests(unittest.TestCase):
    def test_copy_preserves_ids_visibility_vectors_nulls_and_backup_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, backup = (Path(directory) / name for name in ("source.db", "target.db", "backup.db"))
            engine = create_engine(f"sqlite:///{source}")
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE rss_feed_subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id BIGINT, channel_id BIGINT, feed_url TEXT, title TEXT, created_by_id BIGINT, created_at DATETIME)"))
                connection.execute(text("INSERT INTO rss_feed_subscriptions VALUES (1,2,3,'https://example.invalid/feed','archived fixture',4,'2026-01-01 00:00:00')"))
                connection.execute(insert(UserSettings).values(user_id=1505776662762229810, memory_enabled=True))
                connection.execute(insert(Memory), [dict(id=50+i, user_id=1505776662762229810,
                    category="preference", summary="fixture preference", confidence=0.9,
                    visibility=scope, embedding=[0.125, -0.5], tags=["fixture"])
                    for i, scope in enumerate(("private", "guild_shared", "lore"))])
                connection.execute(text("UPDATE memories SET related_entities=NULL WHERE id=50"))
                connection.execute(text("UPDATE memories SET related_entities='null' WHERE id=51"))
                connection.execute(insert(Memory).values(id=999, user_id=1, category="preference", summary="deleted", confidence=1))
                connection.execute(text("DELETE FROM memories WHERE id=999"))
                connection.execute(insert(Reminder).values(channel_id=2, user_id=3, reminder_text="fixture",
                    remind_at=datetime(2027, 1, 1, 5, tzinfo=timezone(timedelta(hours=5)))))
            engine.dispose()
            result = copy_to_sqlite(f"sqlite:///{source}", target)
            self.assertTrue(result["verified"])
            self.assertEqual(3, result["tables"]["memories"]["rows"])
            self.assertEqual(1, result["tables"]["rss_feed_subscriptions"]["rows"])
            self.assertEqual(["rss_feed_subscriptions"], result["archived_legacy_tables"])
            self.assertEqual(999, result["tables"]["memories"]["identity_high_water"])
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            restored = create_engine(f"sqlite:///{target}")
            with restored.begin() as connection:
                self.assertEqual(["private", "guild_shared", "lore"],
                    list(connection.scalars(select(Memory.visibility).order_by(Memory.id))))
                self.assertEqual([0.125, -0.5], connection.scalar(select(Memory.embedding).where(Memory.id == 50)))
                self.assertEqual(1, connection.scalar(text("SELECT related_entities IS NULL FROM memories WHERE id=50")))
                self.assertEqual(0, connection.scalar(text("SELECT related_entities IS NULL FROM memories WHERE id=51")))
                connection.execute(insert(Memory).values(user_id=1, category="preference", summary="new", confidence=1))
                self.assertEqual(1000, connection.scalar(text("SELECT max(id) FROM memories")))
                self.assertEqual(datetime(2027, 1, 1, tzinfo=timezone.utc), connection.scalar(select(Reminder.remind_at)))
            restored.dispose()
            # Keep a committed WAL open while the backup runs: copying just .db would miss it.
            live = sqlite3.connect(target)
            try:
                live.execute("PRAGMA journal_mode=WAL")
                live.execute("INSERT INTO app_state(key,value,updated_at) VALUES ('backup-fixture','latest',CURRENT_TIMESTAMP)")
                live.commit()
                self.assertTrue(backup_sqlite(target, backup)["verified"])
            finally:
                live.close()
            with sqlite3.connect(backup) as connection:
                self.assertEqual("latest", connection.execute("SELECT value FROM app_state WHERE key='backup-fixture'").fetchone()[0])

    def test_refuses_unknown_schema_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = (Path(directory) / name for name in ("source.db", "target.db"))
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE unexpected(value TEXT)")
            with self.assertRaises(ValueError):
                copy_to_sqlite(f"sqlite:///{source}", target)
            self.assertFalse(target.exists())
            self.assertEqual([], list(Path(directory).glob(".nycti-copy-*")))
            target.write_bytes(b"do not replace")
            with self.assertRaises(FileExistsError):
                backup_sqlite(source, target)
            self.assertEqual(b"do not replace", target.read_bytes())


class SQLiteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_runtime_preserves_quotas_reminders_and_memory_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.db"
            restored = Path(directory) / "restored.db"
            config = Settings(discord_token="fixture", openai_api_key="fixture", database_url=f"sqlite:///{source}")
            database = Database(config)
            now = datetime.now(timezone.utc)
            reservation_size = estimate_reservation_tokens([], [], 1)
            try:
                await database.init_models()
                async with database.session() as session:
                    session.add_all([Memory(user_id=owner, guild_id=guild, category="preference",
                        visibility=scope, summary="fixture keyboard preference", confidence=1,
                        embedding=[1.0, 0.0], tags=[]) for owner, guild, scope in
                        ((1, 10, "private"), (2, 10, "private"), (2, 10, "guild_shared"), (2, 20, "lore"))])
                    await ReminderService().create_reminder(session, guild_id=10, channel_id=20,
                        user_id=1, source_message_id=None, reminder_text="fixture due reminder", remind_at=now)
                    await session.commit()
                quota = DailyTokenQuota(database, {"fixture-model": reservation_size}, "fallback", now=lambda: now)
                routes = await asyncio.gather(*(quota.route("fixture-model", max_tokens=1) for _ in range(8)))
                active = [route for route in routes if not route.used_fallback]
                self.assertEqual(1, len(active))
            finally:
                await database.engine.dispose()
            backup_sqlite(source, restored)
            config.database_url = f"sqlite:///{restored}"
            database = Database(config)
            try:
                await database.init_models()
                quota = DailyTokenQuota(database, {"fixture-model": reservation_size}, "fallback", now=lambda: now)
                self.assertTrue(all(await asyncio.gather(*(quota.settle(active[0], total_tokens=7) for _ in range(4)))))
                async with database.session() as session:
                    counter = await session.scalar(select(DailyModelTokenCounter))
                    self.assertEqual((7, 0), (counter.consumed_tokens, counter.reserved_tokens))
                    due = await ReminderService().list_due_reminders(session, due_before=now+timedelta(seconds=1))
                    self.assertEqual(1, len(due))
                    self.assertIsNotNone(due[0].remind_at.tzinfo)
                    matches = await MemoryRetriever(config).retrieve(session, requester_user_id=1,
                        guild_id=10, query="keyboard", query_embedding=[1.0, 0.0])
                    self.assertEqual({(1, "private"), (2, "guild_shared")},
                        {(memory.user_id, memory.visibility) for memory in matches})
                    await session.commit()
            finally:
                await database.engine.dispose()

    async def test_file_database_durability_and_serialized_read_modify_write(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Settings(discord_token="fixture", openai_api_key="fixture",
                database_url=f"sqlite:///{directory}/runtime.db"))
            try:
                await database.init_models()
                async with database.engine.begin() as connection:
                    self.assertEqual("wal", await connection.scalar(text("PRAGMA journal_mode")))
                    self.assertEqual(2, await connection.scalar(text("PRAGMA synchronous")))
                    self.assertEqual(1, await connection.scalar(text("PRAGMA foreign_keys")))
                    await connection.execute(text("INSERT INTO app_state(key,value,updated_at) VALUES ('counter','0',CURRENT_TIMESTAMP)"))

                async def increment():
                    async with database.session() as session:
                        value = int(await session.scalar(text("SELECT value FROM app_state WHERE key='counter'")))
                        await asyncio.sleep(0.001)
                        await session.execute(text("UPDATE app_state SET value=:value WHERE key='counter'"), {"value": str(value+1)})
                        await session.commit()

                await asyncio.gather(*(increment() for _ in range(20)))
                async with database.session() as session:
                    self.assertEqual("20", await session.scalar(text("SELECT value FROM app_state WHERE key='counter'")))
                self.assertEqual(1, database.engine.pool.size())
            finally:
                await database.engine.dispose()
