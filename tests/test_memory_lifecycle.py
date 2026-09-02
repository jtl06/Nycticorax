from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
from typing import cast
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nycti.config import Settings
from nycti.db.models import Base, MemberIdentity, Memory, MemorySnapshot, UserSettings
from nycti.llm.types import LLMResult, LLMUsage
from nycti.memory.extractor import MemoryCandidate
from nycti.memory.lifecycle import (
    MemoryKind,
    MemoryOperation,
    MemoryStatus,
    build_memory_retrieval_plan,
    effective_memory_confidence,
    memory_is_active,
)
from nycti.memory.maintenance import repair_memory_store
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryConsolidationDecision, MemoryService
from nycti.memory.visibility import MemoryVisibility


def _settings(**overrides: object) -> object:
    values = {
        "openai_memory_model": "memory-model",
        "memory_confidence_threshold": 0.78,
        "memory_retrieval_limit": 6,
        "memory_confidence_half_life_days": 365,
        "memory_consolidation_min_memories": 3,
        "memory_consolidation_cooldown_seconds": 21600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _candidate(
    value: str,
    *,
    predicate: str = "preferred_editor",
    operation: MemoryOperation = MemoryOperation.UPSERT,
    kind: MemoryKind = MemoryKind.FACT,
    ttl_days: int | None = None,
    entities: tuple[str, ...] = (),
) -> MemoryCandidate:
    return MemoryCandidate(
        summary=f"Prefers {value}" if value else "",
        category="preference",
        confidence=0.9,
        tags=["editor"],
        source_excerpt="source",
        memory_kind=kind,
        operation=operation,
        predicate=predicate,
        object_text=value,
        related_entities=entities,
        ttl_days=ttl_days,
    )


class _QueuedExtractor:
    def __init__(self, candidates: list[MemoryCandidate], settings: object | None = None) -> None:
        self.candidates = candidates
        self.settings = settings or _settings()

    async def extract(self, **_kwargs):  # type: ignore[no-untyped-def]
        return self.candidates.pop(0), None


class _UnusedLLMClient:
    def is_model_available(self, _model: str) -> bool:
        return True


class _ConsolidationLLMClient(_UnusedLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def complete_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        source_ids = [
            int(value)
            for value in re.findall(r"id=(\d+)", kwargs["messages"][1]["content"])
        ][:3]
        return LLMResult(
            text=(
                '{"should_consolidate":true,'
                '"summary":"Builds Nycti in Python and prefers concise, modular tools",'
                f'"source_ids":{source_ids},'
                '"related_entities":["Nycti","Python"]}'
            ),
            usage=LLMUsage(
                feature="memory_consolidate",
                model="memory-model",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated_cost_usd=0,
            ),
        )


class _FailingEnrichmentLLMClient(_UnusedLLMClient):
    async def complete_chat(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider unavailable")


class MemoryLifecyclePolicyTests(unittest.TestCase):
    def test_dynamic_budget_expands_only_for_memory_heavy_requests(self) -> None:
        self.assertEqual(2, build_memory_retrieval_plan("Explain a deploy", maximum=8).limit)
        self.assertEqual(4, build_memory_retrieval_plan("What is my current project?", maximum=8).limit)
        self.assertEqual(8, build_memory_retrieval_plan("What do you remember about me?", maximum=8).limit)
        self.assertTrue(
            build_memory_retrieval_plan("What editor did I use before?", maximum=8).include_history
        )

    def test_confidence_decays_and_reinforcement_offsets_decay(self) -> None:
        now = datetime.now(timezone.utc)
        recent = SimpleNamespace(
            confidence=0.9,
            memory_kind="fact",
            last_confirmed_at=now,
            reinforcement_count=1,
        )
        stale = SimpleNamespace(
            confidence=0.9,
            memory_kind="fact",
            last_confirmed_at=now - timedelta(days=365),
            reinforcement_count=1,
        )
        reinforced = SimpleNamespace(
            confidence=0.9,
            memory_kind="fact",
            last_confirmed_at=now - timedelta(days=365),
            reinforcement_count=8,
        )

        self.assertGreater(
            effective_memory_confidence(recent, now=now),
            effective_memory_confidence(stale, now=now),
        )
        self.assertGreater(
            effective_memory_confidence(reinforced, now=now),
            effective_memory_confidence(stale, now=now),
        )
        stale_plan = SimpleNamespace(
            confidence=0.9,
            memory_kind="fact",
            category="plan",
            tags=[],
            last_confirmed_at=now - timedelta(days=180),
            reinforcement_count=1,
        )
        self.assertLess(
            effective_memory_confidence(stale_plan, now=now),
            effective_memory_confidence(
                SimpleNamespace(
                    confidence=0.9,
                    memory_kind="fact",
                    category="preference",
                    tags=[],
                    last_confirmed_at=now - timedelta(days=180),
                    reinforcement_count=1,
                ),
                now=now,
            ),
        )

    def test_validity_windows_fail_closed(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertFalse(
            memory_is_active(
                SimpleNamespace(status="active", expires_at=now - timedelta(seconds=1)),
                now=now,
            )
        )
        self.assertFalse(
            memory_is_active(SimpleNamespace(status="superseded"), now=now)
        )


class MemoryLifecycleDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _service(self) -> MemoryService:
        return MemoryService(
            cast(object, _QueuedExtractor([])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings(memory_retrieval_limit=8))),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )

    async def test_materialized_snapshots_keep_private_and_guild_scopes_separate(self) -> None:
        service = self._service()
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    UserSettings(user_id=2, memory_enabled=True),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        summary="User one prefers concise answers",
                        tags=[],
                        confidence=0.9,
                        updated_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=2,
                        visibility="private",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        summary="User two private fact",
                        tags=[],
                        confidence=0.9,
                        updated_at=now,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=2,
                        visibility="lore",
                        category="lore",
                        memory_kind="lore",
                        status="active",
                        summary="Failed deploys are moon launches",
                        tags=["pinned"],
                        confidence=0.9,
                        updated_at=now,
                    ),
                ]
            )
            await session.flush()

            blocks = await service.rebuild_memory_snapshots(
                session,
                user_id=1,
                guild_id=10,
                now=now,
            )

            self.assertIn("User one prefers concise answers", blocks.user)
            self.assertNotIn("User two private fact", blocks.rendered)
            self.assertIn("Failed deploys are moon launches", blocks.guild)
            self.assertEqual(
                2,
                len(list((await session.scalars(select(MemorySnapshot))).all())),
            )
            self.assertEqual(
                3,
                len(list((await session.scalars(select(Memory))).all())),
            )

    async def test_guild_context_falls_back_to_global_user_snapshot(self) -> None:
        service = self._service()
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    Memory(
                        guild_id=None,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        summary="Uses metric units everywhere",
                        tags=[],
                        confidence=0.9,
                        updated_at=now,
                    ),
                ]
            )
            await session.flush()
            await service.rebuild_memory_snapshots(
                session,
                user_id=1,
                guild_id=None,
                now=now,
            )

            blocks = await service.get_memory_snapshot_blocks(
                session,
                user_id=1,
                guild_id=10,
            )

            self.assertIn("Uses metric units everywhere", blocks.user)

    async def test_shared_watchlist_is_visible_cross_user_without_private_leak(self) -> None:
        service = self._service()
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    UserSettings(user_id=2, memory_enabled=True),
                ]
            )
            await session.flush()
            for symbol in ("NVDA", "MU"):
                await service.store_memory_candidate(
                    session,
                    user_id=1,
                    guild_id=10,
                    channel_id=20,
                    source_message_id=30,
                    candidate=MemoryCandidate(
                        summary=f"Include {symbol} in shared market reports",
                        category="preference",
                        confidence=0.95,
                        tags=["stock", "ticker", "shared_watchlist", symbol.casefold()],
                        source_excerpt="Use this shared market watchlist going forward.",
                        suggested_visibility=MemoryVisibility.GUILD_SHARED,
                        predicate=f"shared_market_report_ticker_{symbol.casefold()}",
                        object_text=symbol,
                        related_entities=(symbol.casefold(),),
                    ),
                )
            await service.store_memory_candidate(
                session,
                user_id=1,
                guild_id=10,
                channel_id=20,
                source_message_id=31,
                candidate=_candidate("private-value"),
            )

            matches = await service.search_memories(
                session,
                requester_user_id=2,
                guild_id=10,
                query="What is user 1's shared market watchlist?",
                owner_user_ids=(1,),
                visibility_scopes=("private", "guild_shared"),
                generate_embedding=False,
                limit=8,
            )

        self.assertEqual({"NVDA", "MU"}, {memory.object_text for memory in matches})
        self.assertTrue(all(memory.visibility == "guild_shared" for memory in matches))

    async def test_active_market_watchlist_bypasses_snapshot_ranking_without_leaking_private_rows(
        self,
    ) -> None:
        service = self._service()
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    UserSettings(user_id=2, memory_enabled=True),
                ]
            )
            await session.flush()
            for symbol in ("NVDA", "AMD"):
                await service.store_memory_candidate(
                    session,
                    user_id=1,
                    guild_id=10,
                    channel_id=20,
                    source_message_id=30,
                    candidate=MemoryCandidate(
                        summary=f"Follows {symbol} as a stock ticker of interest",
                        category="preference",
                        confidence=0.95,
                        tags=["stock", "ticker", "watchlist", symbol.casefold()],
                        source_excerpt=f"I follow {symbol}.",
                        predicate=f"stock_ticker_interest_{symbol.casefold()}",
                        object_text=symbol,
                    ),
                )
            for symbol, visibility in (
                ("MU", MemoryVisibility.GUILD_SHARED),
                ("SNDK", MemoryVisibility.GUILD_SHARED),
                ("INTC", MemoryVisibility.PRIVATE),
            ):
                await service.store_memory_candidate(
                    session,
                    user_id=2,
                    guild_id=10,
                    channel_id=20,
                    source_message_id=31,
                    candidate=MemoryCandidate(
                        summary=f"Track {symbol}",
                        category="preference",
                        confidence=0.95,
                        tags=["stock", "ticker", symbol.casefold()],
                        source_excerpt=f"Track {symbol}.",
                        suggested_visibility=visibility,
                        predicate=(
                            f"shared_market_report_ticker_{symbol.casefold()}"
                            if visibility is MemoryVisibility.GUILD_SHARED
                            else f"stock_ticker_interest_{symbol.casefold()}"
                        ),
                        object_text=symbol,
                    ),
                )

            watchlist = await service.get_active_market_watchlist(
                session,
                user_id=1,
                guild_id=10,
            )

        self.assertEqual({"NVDA", "AMD"}, set(watchlist.personal))
        self.assertEqual({"MU", "SNDK"}, set(watchlist.shared))
        self.assertNotIn("INTC", watchlist.symbols)

    async def test_memory_maintenance_scrubs_sensitive_and_promotes_shared_legacy_config(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            settings = UserSettings(
                user_id=1,
                memory_enabled=True,
                personal_profile_md="- Likes concise replies\n- Net worth target is private",
            )
            session.add_all(
                [
                    settings,
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="plan",
                        summary="Net worth target is private",
                        source_excerpt="My net worth target is private.",
                        tags=["goal"],
                        confidence=0.9,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        summary="Include NVDA and MU in future market reports",
                        source_excerpt="Going forward include NVDA and MU in market reports.",
                        tags=["market", "tickers"],
                        confidence=0.9,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        summary="Prefers concise replies",
                        source_excerpt="I prefer concise replies.",
                        tags=["style"],
                        confidence=0.9,
                    ),
                ]
            )
            await session.flush()

            result = await repair_memory_store(session, now=now)
            rows = list((await session.scalars(select(Memory))).all())

        self.assertEqual(1, result.sensitive_memories_deleted)
        self.assertEqual(1, result.sensitive_profile_lines_deleted)
        self.assertEqual(2, result.legacy_memories_normalized)
        self.assertEqual(1, result.shared_configurations_promoted)
        self.assertEqual(2, result.shared_watchlist_symbols_migrated)
        self.assertEqual(1, result.legacy_market_configurations_retired)
        self.assertEqual("- Likes concise replies", settings.personal_profile_md)
        shared = next(row for row in rows if "future market reports" in row.summary)
        self.assertEqual("guild_shared", shared.visibility)
        self.assertTrue(shared.predicate)
        self.assertTrue(shared.subject_key)
        self.assertEqual("consolidated", shared.status)
        self.assertEqual(
            {"NVDA", "MU"},
            {
                row.object_text
                for row in rows
                if row.status == "active"
                and row.predicate.startswith("shared_market_report_ticker_")
            },
        )

    async def test_legacy_watchlist_repair_uses_known_symbols_and_rejects_member_names(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    MemberIdentity(
                        guild_id=10,
                        user_id=2,
                        username="gts81",
                        global_name="",
                        display_name="GTS81",
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        predicate="stock_ticker_interest_sndk",
                        object_text="SNDK",
                        summary="Follows SNDK as a stock ticker of interest",
                        tags=["stock", "ticker"],
                        confidence=0.95,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="guild_shared",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        predicate="shared_market_configuration_1",
                        object_text="legacy",
                        summary="Include AMD and SDNK in future market reports",
                        source_excerpt="Going forward include AMD and SDNK in market reports.",
                        tags=["market"],
                        confidence=0.9,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="guild_shared",
                        category="preference",
                        memory_kind="fact",
                        status="active",
                        predicate="shared_market_configuration_2",
                        object_text="legacy",
                        summary="Considers GTS an important stock to track",
                        source_excerpt="you forgot GTS also",
                        tags=["market"],
                        confidence=0.9,
                    ),
                ]
            )
            await session.flush()

            result = await repair_memory_store(session, now=now)
            rows = list((await session.scalars(select(Memory))).all())

        self.assertEqual(2, result.shared_watchlist_symbols_migrated)
        self.assertEqual(2, result.legacy_market_configurations_retired)
        self.assertEqual(
            {"AMD", "SNDK"},
            {
                row.object_text
                for row in rows
                if row.status == "active"
                and row.predicate.startswith("shared_market_report_ticker_")
            },
        )

    async def test_retention_gives_durable_and_reinforced_memories_longer_windows(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        service = MemoryService(
            cast(object, _QueuedExtractor([])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        memories = [
            Memory(
                guild_id=10,
                user_id=1,
                category="project",
                memory_kind=kind,
                status="active",
                summary=name,
                tags=[],
                confidence=0.9,
                reinforcement_count=reinforcement_count,
                times_retrieved=0,
                created_at=now - timedelta(days=age_days),
                updated_at=now - timedelta(days=activity_age_days),
                last_confirmed_at=(
                    now - timedelta(days=activity_age_days)
                    if activity_age_days != age_days
                    else None
                ),
            )
            for name, kind, reinforcement_count, age_days, activity_age_days in (
                ("durable fact survives", "fact", 1, 300, 300),
                ("old episode expires", "episode", 1, 200, 200),
                ("reinforced episode survives", "episode", 2, 300, 300),
                ("ancient durable fact expires", "fact", 1, 400, 400),
                ("recently reconfirmed fact survives", "fact", 2, 500, 20),
            )
        ]
        async with self.factory() as session:
            session.add_all(memories)
            await session.commit()

            deleted = await service.prune_stale_memories(
                session,
                now=now,
                never_retrieved_older_than_days=180,
                stale_retrieved_older_than_days=365,
            )
            await session.commit()
            remaining = {
                memory.summary for memory in (await session.scalars(select(Memory))).all()
            }

        self.assertEqual(2, deleted)
        self.assertEqual(
            {
                "durable fact survives",
                "reinforced episode survives",
                "recently reconfirmed fact survives",
            },
            remaining,
        )

    async def test_precomputed_candidate_rechecks_memory_opt_in_before_write(self) -> None:
        service = MemoryService(
            cast(object, _QueuedExtractor([])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=False))
            await session.flush()

            stored = await service.store_memory_candidate(
                session,
                user_id=1,
                guild_id=10,
                channel_id=20,
                source_message_id=30,
                candidate=_candidate("Zed"),
            )

            self.assertIsNone(stored)
            self.assertEqual([], list((await session.scalars(select(Memory))).all()))

    async def test_stale_generated_profile_cannot_overwrite_concurrent_change(self) -> None:
        service = MemoryService(
            cast(object, _QueuedExtractor([])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            settings = UserSettings(
                user_id=1,
                memory_enabled=True,
                personal_profile_md="- Uses Vim",
            )
            session.add(settings)
            await session.flush()
            settings.personal_profile_md = "- Uses Helix"
            await session.flush()

            applied = await service.apply_personal_profile_update(
                session,
                user_id=1,
                profile_md="- Uses Zed",
                expected_profile="- Uses Vim",
            )

            self.assertFalse(applied)
            self.assertEqual("- Uses Helix", settings.personal_profile_md)

    async def test_stale_consolidation_source_cannot_apply_obsolete_decision(self) -> None:
        service = MemoryService(
            cast(object, _QueuedExtractor([], settings=_settings())),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            sources = [
                Memory(
                    guild_id=10,
                    user_id=1,
                    visibility="private",
                    category="project",
                    memory_kind="fact",
                    status="active",
                    subject_key="user:1",
                    predicate=f"fact_{index}",
                    object_text=value,
                    summary=value,
                    tags=[],
                    confidence=0.9,
                    valid_from=now,
                    last_confirmed_at=now,
                    updated_at=now,
                )
                for index, value in enumerate(("Builds Nycti", "Uses Python", "Prefers modules"))
            ]
            session.add_all(sources)
            await session.flush()
            plan = await service.prepare_memory_consolidation(
                session,
                user_id=1,
                guild_id=10,
                now=now,
            )
            assert plan is not None
            selected_ids = tuple(source.id for source in plan.sources[:2])
            changed = await session.get(Memory, selected_ids[0])
            assert changed is not None
            changed.object_text = "Builds something else"
            changed.summary = "Builds something else"
            await session.flush()

            consolidated = await service.apply_memory_consolidation(
                session,
                plan=plan,
                decision=MemoryConsolidationDecision(
                    summary="Obsolete overview",
                    source_ids=selected_ids,
                    related_entities=(),
                ),
            )

            self.assertIsNone(consolidated)
            summaries = list(
                (
                    await session.scalars(
                        select(Memory).where(Memory.memory_kind == "summary")
                    )
                ).all()
            )
            self.assertEqual([], summaries)

    async def test_same_fact_reinforces_then_changed_value_supersedes(self) -> None:
        extractor = _QueuedExtractor(
            [_candidate("Helix"), _candidate("Helix"), _candidate("Zed")]
        )
        service = MemoryService(
            cast(object, extractor),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            await session.flush()
            first, _ = await self._store(service, session)
            reinforced, _ = await self._store(service, session)
            replacement, _ = await self._store(service, session)

            assert first is not None and reinforced is not None and replacement is not None
            self.assertEqual(first.id, reinforced.id)
            self.assertEqual(2, reinforced.reinforcement_count)
            self.assertEqual(MemoryStatus.SUPERSEDED.value, first.status)
            self.assertEqual(first.id, replacement.supersedes_id)
            self.assertEqual("Zed", replacement.object_text)
            self.assertEqual(MemoryStatus.ACTIVE.value, replacement.status)

            current_only = await service.retriever.retrieve(
                session,
                requester_user_id=1,
                guild_id=10,
                query="editor",
                owner_user_ids=(1,),
            )
            with_history = await service.retriever.retrieve(
                session,
                requester_user_id=1,
                guild_id=10,
                query="editor before",
                owner_user_ids=(1,),
                include_history=True,
            )
            self.assertEqual([replacement], current_only)
            self.assertEqual(
                {MemoryStatus.ACTIVE.value, MemoryStatus.SUPERSEDED.value},
                {memory.status for memory in with_history},
            )

    async def test_explicit_retraction_deactivates_typed_fact(self) -> None:
        extractor = _QueuedExtractor(
            [
                _candidate("Acme", predicate="employer"),
                _candidate("", predicate="employer", operation=MemoryOperation.RETRACT),
            ]
        )
        service = MemoryService(
            cast(object, extractor),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            await session.flush()
            first, _ = await self._store(service, session)
            retracted, _ = await self._store(service, session)

            assert first is not None and retracted is not None
            self.assertEqual(first.id, retracted.id)
            self.assertEqual(MemoryStatus.RETRACTED.value, retracted.status)
            self.assertIsNotNone(retracted.valid_until)

    async def test_ticker_interests_are_independent_per_ticker_and_user(self) -> None:
        service = MemoryService(
            cast(object, _QueuedExtractor([])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add_all(
                [
                    UserSettings(user_id=1, memory_enabled=True),
                    UserSettings(user_id=2, memory_enabled=True),
                ]
            )
            await session.flush()

            for user_id, symbol in ((1, "NVDA"), (1, "AMD"), (2, "NVDA")):
                stored = await service.store_memory_candidate(
                    session,
                    user_id=user_id,
                    guild_id=10,
                    channel_id=20,
                    source_message_id=30,
                    candidate=_candidate(
                        symbol,
                        predicate=f"stock_ticker_interest_{symbol.casefold()}",
                        entities=(symbol.casefold(),),
                    ),
                )
                self.assertIsNotNone(stored)

            retracted = await service.store_memory_candidate(
                session,
                user_id=1,
                guild_id=10,
                channel_id=20,
                source_message_id=31,
                candidate=_candidate(
                    "",
                    predicate="stock_ticker_interest_nvda",
                    operation=MemoryOperation.RETRACT,
                ),
            )

            assert retracted is not None
            rows = list((await session.scalars(select(Memory))).all())
            active_pairs = {
                (row.user_id, row.object_text)
                for row in rows
                if row.status == MemoryStatus.ACTIVE.value
            }
            self.assertEqual({(1, "AMD"), (2, "NVDA")}, active_pairs)
            self.assertEqual(MemoryStatus.RETRACTED.value, retracted.status)

    async def test_explicit_working_memory_gets_bounded_expiry(self) -> None:
        service = MemoryService(
            cast(object, _QueuedExtractor([_candidate("Helix", kind=MemoryKind.WORKING, ttl_days=7)])),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _UnusedLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            await session.flush()
            memory, _ = await self._store(service, session)

            assert memory is not None and memory.valid_from is not None and memory.expires_at is not None
            self.assertEqual(MemoryKind.WORKING.value, memory.memory_kind)
            self.assertAlmostEqual(
                7,
                (memory.expires_at - memory.valid_from).total_seconds() / 86_400,
                places=2,
            )

    async def test_retrieval_excludes_inactive_and_uses_related_entities(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            related = Memory(
                guild_id=10,
                user_id=1,
                visibility="private",
                category="project",
                memory_kind="fact",
                status="active",
                subject_key="user:1",
                predicate="current_project",
                object_text="Builds a Discord assistant",
                summary="Builds a Discord assistant",
                tags=[],
                related_entities=["nycti"],
                confidence=0.9,
                valid_from=now,
                last_confirmed_at=now,
            )
            session.add_all(
                [
                    related,
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="project",
                        memory_kind="fact",
                        status="superseded",
                        summary="Old Nycti architecture",
                        tags=["nycti"],
                        confidence=1.0,
                    ),
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="project",
                        memory_kind="working",
                        status="active",
                        summary="Temporary Nycti task",
                        tags=["nycti"],
                        confidence=1.0,
                        expires_at=now - timedelta(minutes=1),
                    ),
                ]
            )
            await session.flush()

            selected = await MemoryRetriever(cast(Settings, _settings())).retrieve(
                session,
                requester_user_id=1,
                guild_id=10,
                query="Nycti",
                owner_user_ids=(1,),
            )

            self.assertEqual([related], selected)

    async def test_background_consolidation_creates_bounded_summary_and_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        client = _ConsolidationLLMClient()
        extractor = _QueuedExtractor([], settings=_settings())
        service = MemoryService(
            cast(object, extractor),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, client),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            session.add_all(
                [
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="project",
                        memory_kind="fact",
                        status="active",
                        subject_key="user:1",
                        predicate=predicate,
                        summary=summary,
                        object_text=summary,
                        tags=[],
                        confidence=0.9,
                        valid_from=now,
                        last_confirmed_at=now,
                        updated_at=now,
                    )
                    for predicate, summary in (
                        ("current_project", "Builds Nycti"),
                        ("primary_language", "Uses Python"),
                        ("code_style", "Prefers small modules"),
                    )
                ]
            )
            guild_lore = Memory(
                guild_id=10,
                user_id=1,
                visibility="lore",
                category="lore",
                memory_kind="lore",
                status="active",
                subject_key="guild:10",
                predicate="deploy_name",
                summary="Calls failed deploys moon launches",
                object_text="moon launches",
                tags=[],
                confidence=0.9,
                valid_from=now,
                last_confirmed_at=now,
                updated_at=now,
            )
            session.add(guild_lore)
            await session.flush()

            consolidated, result = await service.maybe_consolidate_memories(
                session,
                user_id=1,
                guild_id=10,
                now=now,
            )
            skipped, skipped_result = await service.maybe_consolidate_memories(
                session,
                user_id=1,
                guild_id=10,
                now=now + timedelta(minutes=1),
            )

            assert consolidated is not None and result is not None
            self.assertEqual(MemoryKind.SUMMARY.value, consolidated.memory_kind)
            self.assertEqual(3, len(consolidated.source_memory_ids or []))
            self.assertNotIn(guild_lore.id, consolidated.source_memory_ids or [])
            self.assertEqual(["nycti", "python"], consolidated.related_entities)
            self.assertIsNone(skipped)
            self.assertIsNone(skipped_result)
            self.assertEqual(1, client.calls)

    async def test_optional_enrichment_failures_do_not_abort_memory_transaction(self) -> None:
        now = datetime.now(timezone.utc)
        extractor = _QueuedExtractor([], settings=_settings())
        service = MemoryService(
            cast(object, extractor),  # type: ignore[arg-type]
            MemoryRetriever(cast(Settings, _settings())),
            llm_client=cast(object, _FailingEnrichmentLLMClient()),  # type: ignore[arg-type]
            embedding_model=None,
        )
        async with self.factory() as session:
            session.add(UserSettings(user_id=1, memory_enabled=True))
            session.add_all(
                [
                    Memory(
                        guild_id=10,
                        user_id=1,
                        visibility="private",
                        category="project",
                        memory_kind="fact",
                        status="active",
                        subject_key="user:1",
                        predicate=f"fact_{index}",
                        summary=f"Durable fact {index}",
                        tags=[],
                        confidence=0.9,
                        valid_from=now,
                        last_confirmed_at=now,
                        updated_at=now,
                    )
                    for index in range(3)
                ]
            )
            await session.flush()

            profile_result = await service.maybe_update_personal_profile(
                session,
                user_id=1,
                guild_id=10,
                channel_id=20,
                current_message="I prefer concise replies.",
                recent_context="",
            )
            consolidated, consolidation_result = await service.maybe_consolidate_memories(
                session,
                user_id=1,
                guild_id=10,
                now=now,
            )
            await session.commit()

            self.assertIsNone(profile_result)
            self.assertIsNone(consolidated)
            self.assertIsNone(consolidation_result)
            self.assertEqual(3, len((await session.scalars(select(Memory))).all()))

    @staticmethod
    async def _store(
        service: MemoryService,
        session: AsyncSession,
    ) -> tuple[Memory | None, object | None]:
        return await service.maybe_store_memory(
            session,
            user_id=1,
            guild_id=10,
            channel_id=20,
            source_message_id=30,
            current_message="durable memory update",
            recent_context="",
        )


if __name__ == "__main__":
    unittest.main()
