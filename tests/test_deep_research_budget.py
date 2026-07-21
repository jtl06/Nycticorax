from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from nycti.chat.run_state import (
    AgentBudget,
    AgentPermissions,
    AgentRun,
    ToolExecutionResult,
    ToolOutcome,
    ToolStatus,
)
from nycti.chat.tool_budget import (
    available_tools_after_budget_skip,
    select_tool_calls_within_budget,
)
from nycti.chat.tool_eligibility import expand_tools_from_outcomes
from nycti.chat.tools.handlers import RegisteredToolHandlerMixin, ToolExecutionContext
from nycti.chat.tools.research import ResearchToolMixin, _specialized_result_succeeded
from nycti.chat.tools.registry import get_tool_spec


class DeepResearchBudgetTests(unittest.TestCase):
    def test_specialized_success_uses_provider_header_not_evidence_wording(self) -> None:
        self.assertTrue(
            _specialized_result_succeeded(
                "Tavily extract for: https://example.com\nThe prior attempt failed in 2024."
            )
        )
        self.assertTrue(
            _specialized_result_succeeded(
                "Twelve Data market quote for: Alpha (ALPHA)\n"
                "Yahoo extended-hours fallback failed."
            )
        )
        self.assertFalse(
            _specialized_result_succeeded("URL extraction failed because the provider timed out.")
        )

    def test_deep_research_has_weighted_cost_and_one_call_batch_limit(self) -> None:
        deep_one = _call("deep-1", "deep_research")
        deep_two = _call("deep-2", "deep_research")
        web = _call("web-1", "web")

        selection = select_tool_calls_within_budget(
            [deep_one, deep_two, web],
            remaining_cost_units=16,
            remaining_deep_research_calls=1,
        )

        self.assertEqual((deep_one, web), selection.executable)
        self.assertEqual(5, selection.cost_units)
        self.assertEqual(1, selection.deep_research_calls)
        self.assertEqual((deep_two,), tuple(call for call, _ in selection.skipped))
        self.assertIn("per-run deep-research limit", selection.skipped[0][1])
        spec = get_tool_spec("deep_research")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(4, spec.budget_cost_units)

    def test_expensive_call_does_not_crowd_out_later_cheap_read(self) -> None:
        deep = _call("deep-1", "deep_research")
        web = _call("web-1", "web")

        selection = select_tool_calls_within_budget(
            [deep, web],
            remaining_cost_units=1,
            remaining_deep_research_calls=1,
        )

        self.assertEqual((web,), selection.executable)
        self.assertEqual((deep,), tuple(call for call, _ in selection.skipped))
        self.assertIn("weighted tool-call budget", selection.skipped[0][1])

    def test_late_deep_research_is_skipped_without_crowding_out_cheap_reads(self) -> None:
        deep = _call("deep-1", "deep_research")
        web = _call("web-1", "web")

        late = select_tool_calls_within_budget(
            [deep, web],
            remaining_cost_units=16,
            remaining_deep_research_calls=1,
            remaining_work_seconds=23.0,
        )
        boundary = select_tool_calls_within_budget(
            [deep],
            remaining_cost_units=16,
            remaining_deep_research_calls=1,
            remaining_work_seconds=25.0,
        )

        self.assertEqual((web,), late.executable)
        self.assertEqual(1, late.cost_units)
        self.assertEqual(0, late.deep_research_calls)
        self.assertEqual((deep,), tuple(call for call, _ in late.skipped))
        self.assertIn("too little work time", late.skipped[0][1])
        self.assertEqual((deep,), boundary.executable)
        self.assertEqual(
            {"web", "quote"},
            available_tools_after_budget_skip(
                {"deep_research", "web", "quote"},
                late,
                remaining_cost_units=16,
            ),
        )
        self.assertEqual(
            set(),
            available_tools_after_budget_skip(
                {"deep_research", "web"},
                late,
                remaining_cost_units=0,
            ),
        )

    def test_invalid_deep_arguments_do_not_consume_the_one_expensive_call_allowance(self) -> None:
        deep = _call("deep-1", "deep_research")
        selection = select_tool_calls_within_budget(
            [deep],
            remaining_cost_units=16,
            remaining_deep_research_calls=1,
        )
        run = AgentRun(messages=[], budget=AgentBudget(max_tool_calls=16))
        outcome = ToolOutcome(
            call_id="deep-1",
            tool_name="deep_research",
            arguments='{"question":"research"}',
            status=ToolStatus.ERROR,
            content="Invalid specialized inputs.",
            metrics={"deep_research_status": "invalid_inputs"},
            retryable=True,
        )

        selection.record_execution(run, [outcome])

        self.assertEqual(1, run.tool_calls)
        self.assertEqual(4, run.tool_cost_units)
        self.assertEqual(0, run.deep_research_calls)
        self.assertEqual(1, run.remaining_deep_research_calls())

    def test_browser_fallback_cannot_escape_runtime_reachability(self) -> None:
        failed_extract = ToolOutcome(
            call_id="extract-1",
            tool_name="url_extract",
            arguments='{"url":"https://example.com"}',
            status=ToolStatus.ERROR,
            content="URL extraction failed.",
        )

        unavailable = expand_tools_from_outcomes(
            {"url_extract"},
            [failed_extract],
            reachable_tool_names={"url_extract"},
        )
        available = expand_tools_from_outcomes(
            {"url_extract"},
            [failed_extract],
            reachable_tool_names={"url_extract", "browser_extract"},
        )

        self.assertNotIn("browser_extract", unavailable)
        self.assertIn("browser_extract", available)


class DeepResearchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_nested_capabilities_are_never_executed(self) -> None:
        executor = _CapabilityGatedResearchExecutor()

        result = await executor._execute_deep_research_tool(
            question="Compare Alpha",
            focus=None,
            urls=("https://example.com/report",),
            symbols=("ALPHA",),
            youtube_urls=("https://youtu.be/example",),
            calculations=("result = 2 + 2",),
            guild_id=1,
            channel_id=2,
            user_id=3,
        )

        self.assertEqual(ToolStatus.OK, result.status)
        self.assertEqual([], executor.nested_calls)
        self.assertEqual(4, result.metrics["deep_research_specialized_unavailable_count"])
        self.assertEqual(0, result.metrics["deep_research_specialized_call_count"])
        self.assertIn("restricted Python is disabled", result.content)

    async def test_shared_handler_semaphore_bounds_concurrent_meta_calls(self) -> None:
        handler = _BlockingDeepResearchHandler(max_concurrency=2)
        context = ToolExecutionContext(
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_message_id=4,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )
        tasks = [
            asyncio.create_task(
                handler._handle_deep_research(
                    f'{{"question":"question {index}"}}',
                    context,
                )
            )
            for index in range(3)
        ]
        await asyncio.wait_for(handler.two_started.wait(), timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(2, handler.started)
        self.assertEqual(2, handler.max_active)

        handler.release.set()
        results = await asyncio.gather(*tasks)

        self.assertEqual(3, handler.started)
        self.assertTrue(all(result.status == ToolStatus.OK for result in results))


class _BlockingDeepResearchHandler(RegisteredToolHandlerMixin):
    def __init__(self, *, max_concurrency: int) -> None:
        self.deep_research_semaphore = asyncio.Semaphore(max_concurrency)
        self.release = asyncio.Event()
        self.two_started = asyncio.Event()
        self.started = 0
        self.active = 0
        self.max_active = 0

    async def _execute_deep_research_tool(self, **_kwargs: object) -> ToolExecutionResult:
        self.started += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.started == 2:
            self.two_started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return ToolExecutionResult(content="Research complete.", status=ToolStatus.OK)


class _CapabilityGatedResearchExecutor(ResearchToolMixin):
    def __init__(self) -> None:
        web_outcome = ToolOutcome(
            call_id="web-1",
            tool_name="deep_research",
            arguments='{"query":"Alpha"}',
            status=ToolStatus.OK,
            content="Web evidence: https://example.com/evidence",
            provenance=("https://example.com/evidence",),
        )
        self.deep_research_service = SimpleNamespace(
            research=_async_result(
                SimpleNamespace(
                    metrics={"deep_research_status": "ok"},
                    usages=(),
                    outcomes=(web_outcome,),
                    status="ok",
                )
            )
        )
        self.nested_calls: list[str] = []

    def available_tool_names(self, **_kwargs: object) -> frozenset[str]:
        return frozenset()

    async def _execute_extract_url_tool(self, **_kwargs: object) -> str:
        self.nested_calls.append("url_extract")
        return "unexpected"

    async def _execute_stock_quote_tool(self, **_kwargs: object) -> str:
        self.nested_calls.append("quote")
        return "unexpected"

    async def _execute_youtube_transcript_tool(self, **_kwargs: object) -> tuple[str, int]:
        self.nested_calls.append("yt_transcript")
        return "unexpected", 0

    async def _execute_python_tool(self, **_kwargs: object) -> str:
        self.nested_calls.append("calc")
        return "unexpected"


def _call(call_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, arguments="{}")


def _async_result(value: object):  # type: ignore[no-untyped-def]
    async def resolve(*_args: object, **_kwargs: object) -> object:
        return value

    return resolve


if __name__ == "__main__":
    unittest.main()
