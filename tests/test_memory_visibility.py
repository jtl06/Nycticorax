from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
import unittest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import Base, Memory, UserSettings
from nycti.db.session import Database
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.extractor import MemoryCandidate
from nycti.memory.service import MemoryService
from nycti.memory.visibility import MemoryVisibility, can_read_memory


class _UnusedExtractor:
    pass


class _UnusedLLMClient:
    pass


class _CandidateExtractor:
    async def extract(self, **_kwargs):  # type: ignore[no-untyped-def]
        return (
            MemoryCandidate(
                summary="Calls broken deploys moon launches",
                category="lore",
                confidence=0.95,
                tags=["deploy"],
                source_excerpt="We always call broken deploys moon launches.",
                suggested_visibility=MemoryVisibility.LORE,
            ),
            None,
        )


def _memory_service(*, limit: int = 10) -> MemoryService:
    retriever_settings = cast(Settings, SimpleNamespace(memory_retrieval_limit=limit))
    return MemoryService(
        cast(object, _UnusedExtractor()),  # type: ignore[arg-type]
        MemoryRetriever(retriever_settings),
        llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
        embedding_model=None,
    )


class MemoryVisibilityPolicyTests(unittest.TestCase):
    def test_private_is_owner_only_and_unknown_scope_fails_closed(self) -> None:
        self.assertTrue(
            can_read_memory(
                visibility="private",
                owner_user_id=1,
                memory_guild_id=10,
                requester_user_id=1,
                requester_guild_id=20,
            )
        )
        self.assertFalse(
            can_read_memory(
                visibility="private",
                owner_user_id=1,
                memory_guild_id=10,
                requester_user_id=2,
                requester_guild_id=10,
            )
        )
        self.assertFalse(
            can_read_memory(
                visibility="public",
                owner_user_id=1,
                memory_guild_id=10,
                requester_user_id=1,
                requester_guild_id=10,
            )
        )

    def test_shared_scopes_require_the_same_guild(self) -> None:
        for visibility in (MemoryVisibility.GUILD_SHARED, MemoryVisibility.LORE):
            with self.subTest(visibility=visibility):
                self.assertTrue(
                    can_read_memory(
                        visibility=visibility,
                        owner_user_id=1,
                        memory_guild_id=10,
                        requester_user_id=2,
                        requester_guild_id=10,
                    )
                )
                self.assertFalse(
                    can_read_memory(
                        visibility=visibility,
                        owner_user_id=1,
                        memory_guild_id=10,
                        requester_user_id=2,
                        requester_guild_id=20,
                    )
                )
                self.assertFalse(
                    can_read_memory(
                        visibility=visibility,
                        owner_user_id=1,
                        memory_guild_id=10,
                        requester_user_id=2,
                        requester_guild_id=None,
                    )
                )


class MemoryVisibilityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_memory_defaults_private(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                memory = Memory(
                    guild_id=10,
                    channel_id=20,
                    user_id=1,
                    category="project",
                    summary="Builds a keyboard",
                    tags=[],
                    confidence=0.9,
                )
                session.add(memory)
                await session.flush()
                self.assertEqual(MemoryVisibility.PRIVATE.value, memory.visibility)
        finally:
            await engine.dispose()

    async def test_lightweight_migration_marks_legacy_rows_private(self) -> None:
        settings = Settings(
            discord_token="discord-token",
            openai_api_key="openai-key",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        database = Database(settings)
        try:
            async with database.engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE memories (id INTEGER PRIMARY KEY, summary TEXT NOT NULL)")
                )
                await connection.execute(
                    text("INSERT INTO memories (id, summary) VALUES (1, 'legacy memory')")
                )

            await database.init_models()

            async with database.engine.connect() as connection:
                visibility = await connection.scalar(
                    text("SELECT visibility FROM memories WHERE id = 1")
                )
            self.assertEqual(MemoryVisibility.PRIVATE.value, visibility)
        finally:
            await database.engine.dispose()

    async def test_search_api_returns_only_requester_visible_enabled_memories(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                session.add_all(
                    [
                        UserSettings(user_id=1, memory_enabled=True),
                        UserSettings(user_id=2, memory_enabled=True),
                        UserSettings(user_id=3, memory_enabled=False),
                    ]
                )
                now = datetime.now(timezone.utc)
                memories = [
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        summary="Likes mechanical keyboards",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=2,
                        visibility="private",
                        category="preference",
                        summary="Owns a private keyboard prototype",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=2,
                        visibility="guild_shared",
                        category="project",
                        summary="Building the shared keyboard guide",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=2,
                        visibility="lore",
                        category="lore",
                        summary="Won the guild keyboard tournament",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                    Memory(
                        guild_id=20,
                        user_id=2,
                        visibility="guild_shared",
                        category="project",
                        summary="Other guild keyboard guide",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=3,
                        visibility="guild_shared",
                        category="project",
                        summary="Disabled owner's keyboard guide",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now,
                    ),
                ]
                session.add_all(memories)
                await session.flush()

                service = _memory_service()
                selected = await service.search_memories(
                    session,
                    requester_user_id=1,
                    guild_id=10,
                    query="keyboard",
                    generate_embedding=False,
                )

                self.assertEqual(
                    {
                        "Likes mechanical keyboards",
                        "Building the shared keyboard guide",
                        "Won the guild keyboard tournament",
                    },
                    {memory.summary for memory in selected},
                )

                automatic = await service.retrieve_relevant(
                    session,
                    user_id=1,
                    requester_user_id=1,
                    guild_id=10,
                    query="keyboard",
                    generate_embedding=False,
                )
                self.assertEqual(
                    {
                        "Likes mechanical keyboards",
                        "Building the shared keyboard guide",
                        "Won the guild keyboard tournament",
                    },
                    {memory.summary for memory in automatic},
                )

                related = await service.retrieve_relevant_for_users(
                    session,
                    user_ids=(2,),
                    requester_user_id=1,
                    guild_id=10,
                    query="keyboard",
                    usage_user_id=1,
                    generate_embedding=False,
                )
                self.assertEqual(
                    {
                        "Building the shared keyboard guide",
                        "Won the guild keyboard tournament",
                    },
                    {memory.summary for memory in related[2]},
                )
        finally:
            await engine.dispose()

    async def test_visibility_changes_are_owner_only_and_guild_bound(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                memory = Memory(
                    guild_id=10,
                    user_id=1,
                    category="lore",
                    summary="Keyboard lore",
                    tags=["keyboard"],
                    confidence=0.9,
                )
                session.add(memory)
                await session.flush()
                service = _memory_service()

                denied_nonowner = await service.set_memory_visibility(
                    session,
                    requester_user_id=2,
                    memory_id=memory.id,
                    visibility="lore",
                    guild_id=10,
                )
                denied_wrong_guild = await service.set_memory_visibility(
                    session,
                    requester_user_id=1,
                    memory_id=memory.id,
                    visibility="lore",
                    guild_id=20,
                )
                updated = await service.set_memory_visibility(
                    session,
                    requester_user_id=1,
                    memory_id=memory.id,
                    visibility="lore",
                    guild_id=10,
                )

                self.assertIsNone(denied_nonowner)
                self.assertIsNone(denied_wrong_guild)
                self.assertIs(memory, updated)
                self.assertEqual("lore", memory.visibility)
        finally:
            await engine.dispose()

    async def test_background_candidate_can_store_explicit_group_lore(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                session.add(UserSettings(user_id=1, memory_enabled=True))
                await session.flush()
                service = MemoryService(
                    cast(object, _CandidateExtractor()),  # type: ignore[arg-type]
                    MemoryRetriever(cast(Settings, SimpleNamespace(memory_retrieval_limit=10))),
                    llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
                    embedding_model=None,
                )

                memory, _ = await service.maybe_store_memory(
                    session,
                    user_id=1,
                    guild_id=10,
                    channel_id=20,
                    source_message_id=30,
                    current_message="We always call broken deploys moon launches.",
                    recent_context="",
                )

                self.assertIsNotNone(memory)
                assert memory is not None
                self.assertEqual(MemoryVisibility.LORE.value, memory.visibility)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
