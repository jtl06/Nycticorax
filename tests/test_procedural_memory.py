from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.db.models import Base, ProcedureMemory
from nycti.llm.types import LLMResult, LLMUsage
from nycti.procedures.background import (
    BackgroundProcedureLearner,
    ProcedureLearningJob,
)
from nycti.procedures.service import (
    PROCEDURE_STATUS_VALIDATED,
    PROCEDURE_STATUS_CANDIDATE,
    ProcedureMemoryService,
    ProcedureCandidate,
    format_procedure_matches,
    parse_procedure_ids,
    parse_tool_names,
)


class _FakeLLMClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.last_kwargs = None

    def is_model_available(self, model: str) -> bool:
        return True

    async def complete_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_kwargs = kwargs
        return LLMResult(
            text=self.payload,
            usage=LLMUsage(
                feature="procedure_extract",
                model="memory-model",
                prompt_tokens=80,
                completion_tokens=40,
                total_tokens=120,
                estimated_cost_usd=0.0,
            ),
        )


class _TestDatabase:
    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory

    def session(self):  # type: ignore[no-untyped-def]
        return self.factory()


class ProceduralMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _service(self, client: _FakeLLMClient) -> ProcedureMemoryService:
        return ProcedureMemoryService(
            settings=SimpleNamespace(openai_memory_model="memory-model"),
            llm_client=client,
        )

    async def test_repeated_execution_success_never_activates_a_candidate(
        self,
    ) -> None:
        client = _FakeLLMClient(
            '{"should_store":true,'
            '"task_pattern":"Explain a current stock-sector move using market data and news",'
            '"steps":["Fetch benchmark and representative quotes concurrently",'
            '"Search once for the current catalyst","Reconcile timestamps before answering"],'
            '"match_terms":["stocks","market move","sector","catalyst","quotes"],'
            '"confidence":0.86}'
        )
        service = self._service(client)
        learner = BackgroundProcedureLearner(
            database=_TestDatabase(self.sessions),
            service=service,
        )
        first = ProcedureLearningJob(
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=4,
            source_run_id="run-1",
            request_text="Why are chip stocks falling today?",
            successful_tools=("quote", "web"),
            selected_procedure_ids=(),
        )
        second = ProcedureLearningJob(
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=5,
            source_run_id="run-2",
            request_text="Why are chip stocks falling today?",
            successful_tools=("quote", "web"),
            selected_procedure_ids=(),
        )
        third = ProcedureLearningJob(
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=6,
            source_run_id="run-3",
            request_text="Why are chip stocks falling today?",
            successful_tools=("quote", "web"),
            selected_procedure_ids=(),
        )

        await learner.run(first)
        await learner.run(second)
        await learner.run(third)

        async with self.sessions() as session:
            row = await session.scalar(select(ProcedureMemory))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(PROCEDURE_STATUS_CANDIDATE, row.status)
            self.assertEqual(3, row.success_count)
            self.assertEqual(3, row.success_streak)
            matches = await service.retrieve(
                session,
                guild_id=1,
                query="Explain today's semiconductor market move.",
            )
            unrelated = await service.retrieve(
                session,
                guild_id=1,
                query="What time is it right now?",
            )
            other_guild = await service.retrieve(
                session,
                guild_id=999,
                query="Explain today's semiconductor market move.",
            )

        self.assertEqual(1, client.calls)
        self.assertEqual([], matches)
        self.assertEqual([], unrelated)
        self.assertEqual([], other_guild)
        rendered = format_procedure_matches(matches)
        self.assertEqual("", rendered)
        self.assertNotIn("chip stocks falling today", rendered)

    async def test_feedback_demotes_an_active_procedure_immediately(self) -> None:
        client = _FakeLLMClient("{}")
        service = self._service(client)
        async with self.sessions() as session:
            row = ProcedureMemory(
                guild_id=1,
                task_key="key",
                task_pattern="Look up current market prices",
                steps=["Fetch current quotes"],
                tool_names=["quote"],
                match_terms=["price", "quote", "market"],
                status=PROCEDURE_STATUS_VALIDATED,
                confidence=0.8,
                success_count=3,
                success_streak=3,
                failure_count=0,
                last_success_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.flush()
            row_id = row.id

            changed = await service.demote(session, procedure_ids=(row_id,))
            await session.commit()

        async with self.sessions() as session:
            demoted = await session.get(ProcedureMemory, row_id)
        self.assertEqual(1, changed)
        self.assertIsNotNone(demoted)
        assert demoted is not None
        self.assertEqual(PROCEDURE_STATUS_CANDIDATE, demoted.status)
        self.assertEqual(1, demoted.failure_count)
        self.assertEqual(0, demoted.success_streak)
        self.assertAlmostEqual(0.55, demoted.confidence)

        async with self.sessions() as session:
            relearning = await session.get(ProcedureMemory, row_id)
            assert relearning is not None
            service.reinforce(relearning)
            self.assertEqual(PROCEDURE_STATUS_CANDIDATE, relearning.status)
            service.reinforce(relearning)
            self.assertEqual(PROCEDURE_STATUS_CANDIDATE, relearning.status)
            service.reinforce(relearning)
            self.assertEqual(PROCEDURE_STATUS_CANDIDATE, relearning.status)

    async def test_legacy_auto_promoted_row_is_not_retrieved_or_reinserted(self) -> None:
        service = self._service(_FakeLLMClient("{}"))
        candidate = ProcedureCandidate(
            task_pattern="Compare market prices with news",
            steps=("Fetch current prices", "Read current news"),
            tool_names=("quote", "web"), match_terms=("market", "prices", "news"), confidence=0.8,
        )
        async with self.sessions() as session:
            row = await service.store_candidate(
                session, guild_id=1, source_message_id=None, source_run_id="first", candidate=candidate,
            )
            row.status = "active"
            await session.flush()
            service.invalidate(1)
            self.assertEqual([], await service.retrieve(session, guild_id=1, query="Compare market prices with news"))
            repeated = await service.store_candidate(
                session, guild_id=1, source_message_id=None, source_run_id="second", candidate=candidate,
            )
            self.assertEqual(row.id, repeated.id)
            self.assertEqual(PROCEDURE_STATUS_CANDIDATE, repeated.status)

    async def test_candidate_prompt_receives_only_value_free_execution_recipe(self) -> None:
        client = _FakeLLMClient('{"should_store":false}')
        service = self._service(client)

        await service.generate_candidate(
            request_text="Why are semis down?",
            successful_tools=("quote", "web"),
            execution_recipe=("batch 1: quote(symbols) + web(queries, topic)",),
        )

        assert client.last_kwargs is not None
        prompt = str(client.last_kwargs["messages"][-1]["content"])
        self.assertIn("quote(symbols) + web(queries, topic)", prompt)
        self.assertNotIn("NVDA", prompt)

    async def test_run_specific_fact_is_rejected(self) -> None:
        client = _FakeLLMClient(
            '{"should_store":true,"task_pattern":"Report NVDA at $123.45",'
            '"steps":["Return that price"],"match_terms":["stock","price"],'
            '"confidence":0.9}'
        )
        service = self._service(client)

        candidate, result = await service.generate_candidate(
            request_text="What is NVDA trading at?",
            successful_tools=("quote",),
        )

        self.assertIsNotNone(result)
        self.assertIsNone(candidate)

    async def test_direct_single_step_tool_does_not_create_learning_work(self) -> None:
        client = _FakeLLMClient("{}")
        learner = BackgroundProcedureLearner(
            database=_TestDatabase(self.sessions),
            service=self._service(client),
        )

        scheduled = learner.schedule(
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=4,
            source_run_id="run-1",
            request_text="What is NVDA trading at?",
            successful_tools=("quote",),
        )

        self.assertFalse(scheduled)
        self.assertEqual(0, learner.pending_count)
        self.assertEqual(0, client.calls)

    def test_metric_value_parsers_are_bounded_and_deduplicated(self) -> None:
        self.assertEqual((4, 7), parse_procedure_ids("4, nope, 7, 4, -1"))
        self.assertEqual(("quote", "web"), parse_tool_names("quote, web, quote"))


if __name__ == "__main__":
    unittest.main()
