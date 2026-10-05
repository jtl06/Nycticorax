from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from nycti.chat.orchestrator_support import looks_structurally_incomplete_answer, should_continue_answer
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import AgentBudget, AgentPermissions, ToolStatus
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.stock_workflows import parse_market_report, parse_watchlist
from nycti.config import Settings
from nycti.db.models import UserSettings
from nycti.db.session import Database, WATCHLIST_SCHEMA_MIGRATION
from nycti.market_reports import build_market_report, select_quote_row
from nycti.llm.types import LLMChatTurn, LLMUsage
from nycti.llm.tool_calls import LLMToolCall
from nycti.memory.service import MemoryService
from nycti.twelvedata.models import TwelveDataQuote
from nycti.yahoo.models import YahooMarketSnapshot

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
SYMBOLS = tuple("NVDA AAPL META GOOG SNDK MU AMD INTC MSFT VT VOO SPCX SKHY WDC STX AVGO ARM".split())


def quote(symbol="NVDA", **overrides):
    values = dict(symbol=symbol, name=symbol, exchange="NASDAQ", instrument_type="Common Stock",
                  currency="USD", datetime="2026-10-05", close=101.0, previous_close=100.0,
                  change=1.0, percent_change=1.0, is_market_open=True)
    return TwelveDataQuote(**(values | overrides))


class Quotes:
    api_key = "fixture"

    def __init__(self):
        self.calls = []
        self.active = self.peak = 0

    async def get_market_quote(self, symbol):
        self.calls.append(symbol)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if symbol == "FAIL":
                raise ValueError("synthetic provider failure")
            return quote(symbol)
        finally:
            self.active -= 1


class StockWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.settings = Settings(discord_token="test", openai_api_key="test",
                                 database_url=f"sqlite+aiosqlite:///{Path(self.temp.name) / 'test.db'}")
        self.database = Database(self.settings)
        await self.database.init_models()
        async with self.database.session() as session:
            session.add_all([UserSettings(user_id=1, memory_enabled=True),
                             UserSettings(user_id=2, memory_enabled=True),
                             UserSettings(user_id=3, memory_enabled=False)])
            await session.commit()
        self.quotes = Quotes()
        self.memory = MemoryService(None, None, llm_client=None, embedding_model=None)
        self.executor = ChatToolExecutor(
            database=self.database, settings=self.settings, llm_client=None,
            market_data_client=self.quotes, tavily_client=None, memory_service=self.memory,
            channel_alias_service=None, reminder_service=None, bot=None,
        )

    async def asyncTearDown(self):
        await self.database.engine.dispose()
        self.temp.cleanup()

    async def call(self, tool, payload, *, user_id=1):
        return await self.executor.execute(tool_name=tool, arguments=json.dumps(payload),
                                           guild_id=9, channel_id=10, source_message_id=100,
                                           user_id=user_id, run_id="test")

    async def test_update_then_market_update_quotes_all_17_and_persists_after_restart(self):
        saved = await self.call("watchlist", {"action": "replace", "symbols": [*SYMBOLS, "AMD"]})
        self.assertEqual(ToolStatus.OK, saved.status)
        await self.database.engine.dispose()
        report = await self.call("market_report", {"symbols": [], "session": "regular"})
        self.assertEqual(set(SYMBOLS), set(self.quotes.calls))
        self.assertEqual(17, report.metrics["market_report_success_count"])
        self.assertEqual(17, len([line for line in report.content.splitlines() if line.startswith("- ")]))
        self.assertIn("2026-10-05", report.content)
        self.assertNotIn("2026-10-05", report.direct_reply)
        self.assertNotIn("provider timestamps", report.direct_reply)
        self.assertLessEqual(self.quotes.peak, 10)
        self.assertGreater(self.quotes.peak, 1)

    async def test_explicit_subset_does_not_expand_and_list_only_does_not_fetch(self):
        await self.call("watchlist", {"action": "replace", "symbols": list(SYMBOLS)})
        listed = await self.call("watchlist", {"action": "get"})
        self.assertEqual(17, listed.metrics["watchlist_symbol_count"])
        self.assertEqual([], self.quotes.calls)
        await self.call("market_report", {"symbols": ["NVDA", "AMD"]})
        self.assertEqual(["NVDA", "AMD"], self.quotes.calls)

    async def test_add_remove_clear_and_user_isolation(self):
        await self.call("watchlist", {"action": "replace", "symbols": ["NVDA", "MU"]})
        await self.call("watchlist", {"action": "add", "symbols": ["AMD", "NVDA"]})
        result = await self.call("watchlist", {"action": "remove", "symbols": ["MU"]})
        self.assertEqual("Saved your watchlist: NVDA, AMD.", result.content)
        other = await self.call("watchlist", {"action": "get"}, user_id=2)
        self.assertNotIn("NVDA", other.content)
        await self.call("watchlist", {"action": "replace", "symbols": []})
        report = await self.call("market_report", {})
        self.assertEqual(ToolStatus.EMPTY, report.status)
        self.assertEqual([], self.quotes.calls)

    async def test_disabled_memory_cannot_write_or_claim_saved(self):
        result = await self.call("watchlist", {"action": "replace", "symbols": ["NVDA"]}, user_id=3)
        self.assertEqual(ToolStatus.ERROR, result.status)
        self.assertFalse(result.direct_reply)

    async def test_failed_commit_does_not_return_success_and_rolls_back(self):
        with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                await self.call("watchlist", {"action": "replace", "symbols": ["NVDA"]})
        async with self.database.session() as session:
            value = await session.scalar(select(UserSettings.market_watchlist).where(UserSettings.user_id == 1))
        self.assertIsNone(value)

    async def test_same_batch_report_observes_write_even_when_emitted_first(self):
        calls = [SimpleNamespace(id="report", name="market_report", arguments="{}"),
                 SimpleNamespace(id="save", name="watchlist", arguments=json.dumps({"action": "replace", "symbols": list(SYMBOLS)}))]
        outcomes = await ToolRunner(self.executor).run(
            calls, guild_id=9, channel_id=10, user_id=1, source_message_id=100,
            permissions=AgentPermissions(), run_id="test", step_index=1,
        )
        self.assertEqual(17, outcomes[0].metrics["market_report_success_count"])
        self.assertTrue(all(outcome.direct_reply for outcome in outcomes))

    async def test_concurrent_adds_do_not_lose_symbols(self):
        await asyncio.gather(*(self.call("watchlist", {"action": "add", "symbols": [symbol]})
                               for symbol in ("NVDA", "AMD", "MU")))
        result = await self.call("watchlist", {"action": "get"})
        self.assertEqual(3, result.metrics["watchlist_symbol_count"])

    async def test_conversation_update_then_quote_uses_one_model_turn_each(self):
        orchestrator = object.__new__(ChatOrchestrator)
        orchestrator.settings = self.settings
        orchestrator.tool_runner = ToolRunner(self.executor)
        orchestrator.agent_budget = AgentBudget()
        for prompt, tool, payload in (
            ("Replace my watchlist with " + ", ".join(SYMBOLS), "watchlist", {"action": "replace", "symbols": list(SYMBOLS)}),
            ("market update", "market_report", {"symbols": [], "session": "latest"}),
        ):
            turn = LLMChatTurn(text="", raw_text="", reasoning_content="", finish_reason="tool_calls",
                              usage=LLMUsage("chat_reply", "fixture", 10, 10, 20, 0),
                              tool_calls=[LLMToolCall(id="call1", name=tool, arguments=json.dumps(payload | {"finish": True}))])
            llm = SimpleNamespace(complete_chat_turn=AsyncMock(return_value=turn))
            orchestrator.llm_client = llm
            metrics = {}
            answer, _ = await orchestrator.run_chat_with_tools(
                chat_model="fixture", messages=[{"role": "user", "content": prompt}],
                guild_id=9, channel_id=10, user_id=1, source_message_id=100,
                request_text=prompt, metrics=metrics,
            )
            self.assertEqual(1, llm.complete_chat_turn.await_count)
            self.assertEqual(1, metrics["server_rendered_reply_count"])
            for symbol in SYMBOLS:
                self.assertIn(symbol, answer)

    async def test_failed_edit_blocks_same_batch_report_of_old_list(self):
        calls = [SimpleNamespace(id="save", name="watchlist", arguments='{"action":"add","symbols":["NVDA"]}'),
                 SimpleNamespace(id="report", name="market_report", arguments="{}")]
        outcomes = await ToolRunner(self.executor).run(
            calls, guild_id=9, channel_id=10, user_id=3, source_message_id=100,
            permissions=AgentPermissions(), run_id="test", step_index=1,
        )
        self.assertTrue(all(outcome.status == ToolStatus.ERROR for outcome in outcomes))
        self.assertEqual([], self.quotes.calls)

    async def test_save_and_quote_continues_after_receipt_and_delivers_exact_report(self):
        orchestrator = object.__new__(ChatOrchestrator)
        orchestrator.settings = self.settings
        orchestrator.tool_runner = ToolRunner(self.executor)
        orchestrator.agent_budget = AgentBudget()
        turns = []
        for tool, payload in (
            ("watchlist", {"action": "replace", "symbols": list(SYMBOLS), "finish": False}),
            ("market_report", {"symbols": [], "session": "regular", "finish": True}),
        ):
            turns.append(LLMChatTurn(text="", raw_text="", reasoning_content="", finish_reason="tool_calls",
                usage=LLMUsage("chat_reply", "fixture", 10, 10, 20, 0),
                tool_calls=[LLMToolCall(id=tool, name=tool, arguments=json.dumps(payload))]))
        llm = SimpleNamespace(complete_chat_turn=AsyncMock(side_effect=turns))
        orchestrator.llm_client = llm
        answer, _ = await orchestrator.run_chat_with_tools(
            chat_model="fixture", messages=[{"role": "user", "content": "Save this list and quote it."}],
            guild_id=9, channel_id=10, user_id=1, source_message_id=100,
            request_text="Save this list and quote it.", metrics={},
        )
        self.assertEqual(2, llm.complete_chat_turn.await_count)
        self.assertIn("Saved your watchlist", answer)
        self.assertEqual(set(SYMBOLS), set(self.quotes.calls))
        self.assertEqual(17, len([line for line in answer.splitlines() if line.startswith("- ")]))

    async def test_existing_database_migration_is_idempotent_and_preserves_settings(self):
        async with self.database.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE user_settings DROP COLUMN market_watchlist"))
            await connection.execute(text("DELETE FROM schema_migrations WHERE name=:name"), {"name": WATCHLIST_SCHEMA_MIGRATION})
        await self.database.init_models()
        await self.database.init_models()
        async with self.database.session() as session:
            settings = await session.scalar(select(UserSettings).where(UserSettings.user_id == 1))
            self.assertTrue(settings.memory_enabled)
            self.assertIsNone(settings.market_watchlist)


class ReportRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_keeps_every_row_and_exact_direction(self):
        report = await build_market_report(("NVDA", "FAIL", "AMD"), requested_session="latest",
                                           primary=Quotes(), yahoo=None, now=NOW)
        rendered = report.render()
        self.assertEqual(2, report.success_count)
        self.assertIn("FAIL: unavailable", rendered)
        self.assertIn("USD 101.00 | +1.00% vs prev close", rendered)
        self.assertIn("\U0001f7e2 NVDA", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_extended_session_uses_same_provider_basis_and_never_wrong_session(self):
        snapshot = YahooMarketSnapshot(symbol="NVDA", currency="USD", regular_price=200,
            regular_previous_close=190, regular_timestamp=int(NOW.timestamp()) - 3600,
            extended_price=198, extended_timestamp=int(NOW.timestamp()) - 30, extended_session="pre")
        row = select_quote_row("NVDA", quote(close=100), snapshot, requested_session="pre", now=NOW)
        self.assertEqual(-1, round(row.percent))
        self.assertIn("\U0001f534", row.render())
        self.assertIn("vs regular close", row.render())
        unavailable = select_quote_row("NVDA", quote(), snapshot, requested_session="overnight", now=NOW)
        self.assertIsNone(unavailable.price)

    def test_stale_extended_quote_and_wrong_identity_are_not_reported_as_current(self):
        stale = YahooMarketSnapshot(symbol="NVDA", extended_price=198, extended_session="overnight",
                                    extended_timestamp=int(NOW.timestamp()) - 86400)
        self.assertIsNone(select_quote_row("NVDA", quote(), stale, requested_session="overnight", now=NOW).price)
        self.assertIsNone(select_quote_row("NVDA", quote("AMD"), None, requested_session="latest", now=NOW).price)
        stale_regular = YahooMarketSnapshot(symbol="NVDA", regular_price=20, regular_timestamp=int(NOW.timestamp()) - 86400)
        self.assertEqual(101, select_quote_row("NVDA", quote(), stale_regular, requested_session="latest", now=NOW).price)

    async def test_yahoo_regular_fallback_survives_primary_failure(self):
        class Yahoo:
            async def get_market_snapshot(self, symbol):
                return YahooMarketSnapshot(symbol=symbol, currency="USD", regular_price=99,
                    regular_previous_close=100, regular_timestamp=int(NOW.timestamp()), market_state="REGULAR")
        report = await build_market_report(("FAIL",), requested_session="latest", primary=Quotes(), yahoo=Yahoo(), now=NOW)
        self.assertEqual(1, report.success_count)
        self.assertIn("-1.00% vs prev close", report.render())

    def test_contracts_reject_owner_injection_malformed_lists_and_overflow(self):
        for value in ({"action": "get", "user_id": 2}, {"action": "replace", "symbols": {}},
                      {"action": "replace", "symbols": None}, {"action": []},
                      {"action": "replace", "symbols": ["NVDA"] * 41},
                      {"action": "replace", "symbols": ["some secret text"]}):
            self.assertIsNone(parse_watchlist(json.dumps(value)))
        self.assertIsNone(parse_market_report('{"session":"yesterday"}'))

    def test_ansi_complete_answer_does_not_request_continuation(self):
        answer = "```ansi\n\x1b[32m●\x1b[0m NVDA 101.00 +1.00%\n```"
        turn = SimpleNamespace(text=answer, finish_reason="stop", usage=SimpleNamespace(completion_tokens=100))
        self.assertFalse(should_continue_answer(turn, max_tokens=8192))
        self.assertTrue(looks_structurally_incomplete_answer(answer + "\nDetails: ["))
        self.assertTrue(looks_structurally_incomplete_answer("```ansi\n\x1b[32m unfinished"))
