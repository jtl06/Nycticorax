from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace
from typing import cast
import unittest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import Base, Memory
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.scoring import cosine_similarity


class _ScalarResult:
    def __init__(self, memories: list[Memory]) -> None:
        self.memories = memories

    def all(self) -> list[Memory]:
        return self.memories


class _MemorySession:
    def __init__(self, memories: list[Memory]) -> None:
        self.memories = memories

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self.memories)


def _memory(
    summary: str,
    *,
    user_id: int = 123,
    guild_id: int | None = 456,
    visibility: str = "private",
    category: str = "project",
    confidence: float = 1.0,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
) -> Memory:
    return cast(
        Memory,
        SimpleNamespace(
            user_id=user_id,
            guild_id=guild_id,
            visibility=visibility,
            summary=summary,
            category=category,
            confidence=confidence,
            tags=tags or [],
            embedding=embedding,
            created_at=datetime.now(timezone.utc),
            times_retrieved=0,
            last_retrieved_at=None,
        ),
    )


def _retriever(*, limit: int = 4) -> MemoryRetriever:
    settings = cast(Settings, SimpleNamespace(memory_retrieval_limit=limit))
    return MemoryRetriever(settings)


def _embedding_with_cosine(similarity: float) -> list[float]:
    return [similarity, math.sqrt(1.0 - (similarity * similarity))]


class MemoryRetrieverTests(unittest.TestCase):
    def test_cosine_similarity_prefers_aligned_vectors(self) -> None:
        high = cosine_similarity([1.0, 0.0], [1.0, 0.0])
        low = cosine_similarity([1.0, 0.0], [0.0, 1.0])
        self.assertGreater(high, low)

    def test_cosine_similarity_handles_missing_vectors(self) -> None:
        self.assertEqual(cosine_similarity(None, [1.0, 0.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], None), 0.0)
        self.assertEqual(cosine_similarity([1.0], [1.0, 2.0]), 0.0)


class MemoryRetrieverRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_normalizes_timestamp_reloaded_from_sqlite(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            async with factory() as session:
                session.add(
                    Memory(
                        guild_id=456,
                        user_id=123,
                        category="preference",
                        summary="Uses a mechanical keyboard",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()

            async with factory() as reopened_session:
                selected = await _retriever().retrieve(
                    reopened_session,
                    requester_user_id=123,
                    owner_user_ids=(123,),
                    guild_id=456,
                    query="mechanical keyboard",
                )

            self.assertEqual(1, len(selected))
            self.assertEqual("Uses a mechanical keyboard", selected[0].summary)
        finally:
            await engine.dispose()

    async def test_private_candidate_is_not_starved_by_newer_guild_candidates(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add(
                    Memory(
                        guild_id=456,
                        user_id=123,
                        visibility="private",
                        category="preference",
                        summary="Uses a mechanical keyboard",
                        tags=["keyboard"],
                        confidence=0.9,
                        created_at=now - timedelta(days=1),
                    )
                )
                session.add_all(
                    Memory(
                        guild_id=456,
                        user_id=999,
                        visibility="guild_shared",
                        category="project",
                        summary=f"Newer unrelated tea note {index}",
                        tags=["beverage"],
                        confidence=0.9,
                        created_at=now + timedelta(microseconds=index),
                    )
                    for index in range(126)
                )
                await session.commit()

            async with factory() as reopened_session:
                selected = await _retriever(limit=1).retrieve(
                    reopened_session,
                    requester_user_id=123,
                    guild_id=456,
                    query="mechanical keyboard",
                )

            self.assertEqual(1, len(selected))
            self.assertEqual("Uses a mechanical keyboard", selected[0].summary)
        finally:
            await engine.dispose()

    async def test_private_memory_is_visible_only_to_its_owner(self) -> None:
        own_private = _memory("Own keyboard preference")
        other_private = _memory("Other keyboard preference", user_id=999)

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([own_private, other_private])),
            requester_user_id=123,
            guild_id=456,
            query="keyboard preference",
        )

        self.assertEqual([own_private], selected)
        self.assertEqual(0, other_private.times_retrieved)

    async def test_guild_shared_and_lore_are_visible_only_in_their_guild(self) -> None:
        guild_shared = _memory(
            "Shared keyboard recommendation",
            user_id=999,
            visibility="guild_shared",
        )
        lore = _memory(
            "Keyboard tournament server lore",
            user_id=888,
            visibility="lore",
        )
        wrong_guild = _memory(
            "Other guild keyboard recommendation",
            user_id=777,
            guild_id=654,
            visibility="guild_shared",
        )

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([guild_shared, lore, wrong_guild])),
            requester_user_id=123,
            guild_id=456,
            query="keyboard",
        )

        self.assertCountEqual([guild_shared, lore], selected)
        self.assertEqual(0, wrong_guild.times_retrieved)

    async def test_visibility_scope_filter_only_narrows_access(self) -> None:
        own_private = _memory("Private keyboard preference")
        guild_shared = _memory(
            "Shared keyboard preference",
            user_id=999,
            visibility="guild_shared",
        )

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([own_private, guild_shared])),
            requester_user_id=123,
            guild_id=456,
            query="keyboard preference",
            visibility_scopes=("guild_shared",),
        )

        self.assertEqual([guild_shared], selected)
        self.assertEqual(0, own_private.times_retrieved)

    async def test_priors_cannot_qualify_a_memory_without_query_relevance(self) -> None:
        unrelated_preference = _memory(
            "Prefers tea with oat milk",
            category="preference",
            confidence=1.0,
            tags=["beverages"],
        )

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([unrelated_preference])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="What keyboard do I use?",
            query_embedding=None,
        )

        self.assertEqual([], selected)
        self.assertEqual(0, unrelated_preference.times_retrieved)
        self.assertIsNone(unrelated_preference.last_retrieved_at)

    async def test_priors_cannot_qualify_a_weak_positive_semantic_match(self) -> None:
        weak_semantic_decoy = _memory(
            "Prefers tea with oat milk",
            category="preference",
            confidence=1.0,
            tags=["beverages"],
            embedding=[0.1, 0.995],
        )

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([weak_semantic_decoy])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="What keyboard do I use?",
            query_embedding=[1.0, 0.0],
        )

        self.assertEqual([], selected)
        self.assertEqual(0, weak_semantic_decoy.times_retrieved)
        self.assertIsNone(weak_semantic_decoy.last_retrieved_at)

    async def test_semantic_memory_beats_unrelated_high_bonus_preference(self) -> None:
        unrelated_preference = _memory(
            "Prefers tea with oat milk",
            category="preference",
            confidence=1.0,
            tags=["beverages"],
            embedding=[0.0, 1.0],
        )
        semantic_match = _memory(
            "Uses a split ergonomic input device at the workstation",
            confidence=0.8,
            tags=["hardware"],
            embedding=[1.0, 0.0],
        )

        selected = await _retriever(limit=1).retrieve(
            cast(AsyncSession, _MemorySession([unrelated_preference, semantic_match])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="What keyboard do I use?",
            query_embedding=[1.0, 0.0],
        )

        self.assertEqual([semantic_match], selected)
        self.assertEqual(0, unrelated_preference.times_retrieved)
        self.assertEqual(1, semantic_match.times_retrieved)

    async def test_relevance_outranks_recency_category_and_confidence_priors(self) -> None:
        decoy_preference = _memory(
            "Prefers tea with oat milk",
            category="preference",
            confidence=1.0,
            tags=["beverages"],
            embedding=_embedding_with_cosine(0.30),
        )
        relevant_project = _memory(
            "Uses a split ergonomic input device at the workstation",
            category="project",
            confidence=0.2,
            tags=["hardware"],
            embedding=_embedding_with_cosine(0.45),
        )
        relevant_project.created_at = datetime.now(timezone.utc) - timedelta(days=365)

        selected = await _retriever(limit=1).retrieve(
            cast(AsyncSession, _MemorySession([decoy_preference, relevant_project])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="What keyboard do I use?",
            query_embedding=[1.0, 0.0],
        )

        self.assertEqual([relevant_project], selected)
        self.assertEqual(0, decoy_preference.times_retrieved)
        self.assertEqual(1, relevant_project.times_retrieved)

    async def test_missing_embedding_does_not_penalize_exact_lexical_match(self) -> None:
        lexical_match = _memory(
            "mechanical keyboard",
            category="preference",
            embedding=None,
        )
        semantic_decoys = [
            _memory(
                f"Unrelated stored detail number {index}",
                embedding=_embedding_with_cosine(0.40),
            )
            for index in range(4)
        ]

        selected = await _retriever(limit=4).retrieve(
            cast(AsyncSession, _MemorySession([lexical_match, *semantic_decoys])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="mechanical keyboard",
            query_embedding=[1.0, 0.0],
        )

        self.assertEqual(lexical_match, selected[0])
        self.assertIn(lexical_match, selected)
        self.assertEqual(1, lexical_match.times_retrieved)
        self.assertEqual(3, sum(memory.times_retrieved for memory in semantic_decoys))

    async def test_lexical_retrieval_remains_available_without_an_embedding(self) -> None:
        lexical_match = _memory(
            "Uses a mechanical keyboard at work",
            category="preference",
            tags=["hardware"],
        )

        selected = await _retriever().retrieve(
            cast(AsyncSession, _MemorySession([lexical_match])),
            requester_user_id=123,
            owner_user_ids=(123,),
            guild_id=456,
            query="What keyboard do I use?",
            query_embedding=None,
        )

        self.assertEqual([lexical_match], selected)
        self.assertEqual(1, lexical_match.times_retrieved)


if __name__ == "__main__":
    unittest.main()
