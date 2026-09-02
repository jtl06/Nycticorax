from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nycti.memory.background import BackgroundMemoryWriter


class _FakeSession:
    def __init__(self, database: "_FakeDatabase") -> None:
        self.database = database

    async def commit(self) -> None:
        self.database.commits += 1


class _FakeDatabase:
    def __init__(self) -> None:
        self.active_sessions = 0
        self.commits = 0

    @asynccontextmanager
    async def session(self):
        self.active_sessions += 1
        try:
            yield _FakeSession(self)
        finally:
            self.active_sessions -= 1


class _FakeMemoryService:
    def __init__(self, database: _FakeDatabase, *, enabled: bool = True) -> None:
        self.database = database
        self.enabled = enabled
        self.store_users: list[int] = []
        self.profile_users: list[int] = []
        self.consolidate_users: list[int] = []
        self.extraction_calls = 0
        self.correction_contexts: list[bool] = []

    async def is_enabled(self, _session, _user_id: int) -> bool:
        return self.enabled

    async def generate_memory_candidate(self, **kwargs):
        self.assert_no_active_session()
        self.extraction_calls += 1
        self.correction_contexts.append(bool(kwargs.get("correction_context")))
        return SimpleNamespace(summary="Prefers dark mode"), None

    async def store_memory_candidate(self, _session, *, user_id: int, **_kwargs):
        self.store_users.append(user_id)
        return SimpleNamespace(id=1, embedding=[])

    async def get_personal_profile_md(self, _session, _user_id: int) -> str:
        return ""

    async def generate_personal_profile_update(self, **_kwargs):
        self.assert_no_active_session()
        self.profile_users.append(1)
        return None, None

    async def prepare_memory_consolidation(self, _session, *, user_id: int, **_kwargs):
        self.consolidate_users.append(user_id)
        return None

    def assert_no_active_session(self) -> None:
        if self.database.active_sessions:
            raise AssertionError("external provider work ran while a database session was open")


class _OptOutAfterStoreMemoryService(_FakeMemoryService):
    def __init__(self, database: _FakeDatabase) -> None:
        super().__init__(database)
        self.embedding_calls = 0
        self.consolidation_calls = 0

    async def store_memory_candidate(self, _session, *, user_id: int, **_kwargs):
        self.store_users.append(user_id)
        self.enabled = False
        return SimpleNamespace(id=1, embedding=None)

    async def memory_embedding_target_is_current(self, _session, **_kwargs) -> bool:
        return self.enabled

    async def generate_memory_storage_embedding(self, _summary: str):
        self.embedding_calls += 1
        return None

    async def prepare_memory_consolidation(self, _session, *, user_id: int, **_kwargs):
        self.consolidate_users.append(user_id)
        return object() if self.enabled else None

    async def generate_memory_consolidation(self, _plan):
        self.consolidation_calls += 1
        return None, None


class _BlockingMemoryService(_FakeMemoryService):
    def __init__(self, database: _FakeDatabase) -> None:
        super().__init__(database)
        self.extraction_started = asyncio.Event()
        self.release_extraction = asyncio.Event()
        self.active_extractions = 0
        self.max_active_extractions = 0

    async def generate_memory_candidate(self, **_kwargs):
        self.assert_no_active_session()
        self.extraction_calls += 1
        self.active_extractions += 1
        self.max_active_extractions = max(
            self.max_active_extractions,
            self.active_extractions,
        )
        self.extraction_started.set()
        try:
            await self.release_extraction.wait()
        finally:
            self.active_extractions -= 1
        return SimpleNamespace(summary="Prefers dark mode"), None


class _MultiCandidateMemoryService(_FakeMemoryService):
    async def generate_memory_candidates(self, **_kwargs):
        self.assert_no_active_session()
        self.extraction_calls += 1
        return (
            [
                SimpleNamespace(
                    summary="Follows NVDA",
                    predicate="stock_ticker_interest_nvda",
                ),
                SimpleNamespace(
                    summary="Follows AMD",
                    predicate="stock_ticker_interest_amd",
                ),
            ],
            None,
        )


class BackgroundMemoryWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_classification_can_store_multiple_ticker_facts(self) -> None:
        database = _FakeDatabase()
        memory_service = _MultiCandidateMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="I follow NVDA and AMD.",
            recent_context="",
        )

        self.assertEqual(1, memory_service.extraction_calls)
        self.assertEqual([1, 1], memory_service.store_users)
        self.assertEqual([], memory_service.profile_users)

    async def test_durable_caller_signal_does_not_update_mentioned_user_profile(self) -> None:
        database = _FakeDatabase()
        memory_service = _FakeMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="I prefer dark mode, unlike GTS.",
            recent_context="",
        )

        self.assertEqual([1], memory_service.store_users)
        self.assertEqual([1], memory_service.profile_users)
        self.assertEqual([1], memory_service.consolidate_users)
        self.assertGreaterEqual(database.commits, 2)
        self.assertEqual(0, database.active_sessions)
        self.assertEqual({}, writer._user_locks)

    async def test_ordinary_question_skips_database_and_model_work(self) -> None:
        database = _FakeDatabase()
        memory_service = _FakeMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="What is OpenAI's newest model?",
            recent_context="",
        )

        self.assertEqual(0, memory_service.extraction_calls)
        self.assertEqual(0, database.commits)

    async def test_schedule_does_not_create_task_for_ordinary_question(self) -> None:
        database = _FakeDatabase()
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=_FakeMemoryService(database),
        )

        with patch("nycti.memory.background.asyncio.create_task") as create_task:
            writer.schedule(
                guild_id=1,
                channel_id=2,
                user_id=1,
                source_message_id=3,
                current_message="Summarize this answer.",
                recent_context="",
            )

        create_task.assert_not_called()

    async def test_reported_correction_bypasses_only_the_cost_gate(self) -> None:
        database = _FakeDatabase()
        memory_service = _FakeMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="suxx2succ",
            recent_context="GTS: What would Lucis say?",
            correction_context=True,
        )

        self.assertEqual([True], memory_service.correction_contexts)
        self.assertEqual([1], memory_service.store_users)
        self.assertEqual([], memory_service.profile_users)

    async def test_opt_out_before_enrichment_skips_all_later_provider_calls(self) -> None:
        database = _FakeDatabase()
        memory_service = _OptOutAfterStoreMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="I prefer dark mode.",
            recent_context="",
        )

        self.assertEqual(0, memory_service.embedding_calls)
        self.assertEqual([], memory_service.profile_users)
        self.assertEqual(0, memory_service.consolidation_calls)

    async def test_per_user_lock_serializes_and_cleans_up_waiters(self) -> None:
        database = _FakeDatabase()
        memory_service = _BlockingMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
        )
        kwargs = {
            "guild_id": 1,
            "channel_id": 2,
            "user_id": 1,
            "source_message_id": 3,
            "current_message": "I prefer dark mode.",
            "recent_context": "",
        }

        first = asyncio.create_task(writer.run(**kwargs))
        await memory_service.extraction_started.wait()
        second = asyncio.create_task(writer.run(**kwargs))
        for _ in range(10):
            await asyncio.sleep(0)
            entry = writer._user_locks.get(1)
            if entry is not None and entry.references == 2:
                break
        entry = writer._user_locks.get(1)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(2, entry.references)

        memory_service.release_extraction.set()
        await asyncio.gather(first, second)

        self.assertEqual(1, memory_service.max_active_extractions)
        self.assertEqual({}, writer._user_locks)

    async def test_schedule_uses_one_background_worker_and_drains_fifo_queue(self) -> None:
        database = _FakeDatabase()
        memory_service = _BlockingMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
            queue_maxsize=2,
        )
        first = {
            "guild_id": 1,
            "channel_id": 2,
            "user_id": 1,
            "source_message_id": 3,
            "current_message": "I prefer dark mode.",
            "recent_context": "",
        }
        second = {
            **first,
            "source_message_id": 4,
            "current_message": "I follow NVDA.",
        }

        self.assertTrue(writer.schedule(**first))
        await memory_service.extraction_started.wait()
        self.assertTrue(writer.schedule(**second))
        self.assertEqual(1, writer.pending_count)

        memory_service.release_extraction.set()
        await asyncio.wait_for(writer.join(), timeout=1)
        await writer.close()

        self.assertEqual(2, memory_service.extraction_calls)
        self.assertEqual(1, memory_service.max_active_extractions)
        self.assertEqual(0, writer.pending_count)

    async def test_full_queue_drops_new_optional_work_without_blocking_reply(self) -> None:
        database = _FakeDatabase()
        memory_service = _BlockingMemoryService(database)
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
            queue_maxsize=1,
        )
        kwargs = {
            "guild_id": 1,
            "channel_id": 2,
            "user_id": 1,
            "source_message_id": 3,
            "current_message": "I prefer dark mode.",
            "recent_context": "",
        }

        self.assertTrue(writer.schedule(**kwargs))
        self.assertFalse(writer.schedule(**{**kwargs, "source_message_id": 4}))

        await memory_service.extraction_started.wait()
        memory_service.release_extraction.set()
        await asyncio.wait_for(writer.join(), timeout=1)
        await writer.close()

        self.assertEqual(1, memory_service.extraction_calls)


if __name__ == "__main__":
    unittest.main()
