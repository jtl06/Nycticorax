from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest

from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import (
    AgentBudget,
    AgentPermissions,
    AgentRun,
    StopReason,
    ToolOutcome,
    ToolStatus,
)
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.chat.tool_eligibility import (
    READ_ONLY_TOOL_NAMES,
    expand_tools_from_outcomes,
    select_eligible_tools,
)
from nycti.chat.tool_eligibility import required_tools_for_request
from nycti.chat.tool_runner import ToolRunner


class ToolFallbackTests(unittest.TestCase):
    def test_channel_context_is_not_dumped(self) -> None:
        result = fallback_tool_result(
            "Older Discord channel context (raw, oldest to newest):\n"
            "[2026-04-13 04:00 UTC] mat: one\n"
            "[2026-04-13 04:01 UTC] lucis: two"
        )
        self.assertIn("couldn't produce a clean final reply", result)
        self.assertNotIn("mat: one", result)

    def test_tavily_dump_is_sanitized(self) -> None:
        result = fallback_tool_result(
            "Tavily web results for: nvda earnings\n\n1. Headline\nhttps://example.com\nsnippet"
        )
        self.assertIn("couldn't finish the normal synthesis", result)
        self.assertIn("Headline: snippet", result)
        self.assertIn("[Headline](https://example.com)", result)
        self.assertNotIn("Tavily web results for:", result)
        self.assertNotIn("Unsynthesized snippets", result)

    def test_tavily_memory_stock_fallback_answers_from_source_signal(self) -> None:
        result = fallback_tool_result(
            "Tavily web results for: Samsung Electronics stock drop January 2025 earnings results\n\n"
            "1. Chip Stocks Tumble Again, Jobs Cool and Rivian Rallies - WSJ\n"
            "https://www.wsj.com/finance/investing/chip-stocks-tumble-again-fb8cb62e\n"
            "Memory stocks were particularly hard hit, with Sandisk falling 14.1% and Samsung "
            "Electronics declining 9.1% overnight. Other big tech companies fell as well."
        )
        self.assertIn("commodity cycle", result)
        self.assertIn("peak pricing", result)
        self.assertIn("wsj.com", result)
        self.assertNotIn("Tavily web results for:", result)
        self.assertNotIn("Unsynthesized snippets", result)


class AgentRunTests(unittest.TestCase):
    def test_state_and_budget_are_typed(self) -> None:
        run = AgentRun(messages=[], budget=AgentBudget(max_model_turns=2, max_tool_calls=1))
        self.assertTrue(run.can_start_model_turn())
        self.assertEqual(1, run.remaining_tool_calls())
        run.stop_reason = StopReason.FINAL_TEXT
        self.assertEqual("final_text", run.stop_reason)

    def test_action_tools_require_matching_request_intent(self) -> None:
        ordinary, permissions = select_eligible_tools(
            request_text="What is NVIDIA trading at?",
            search_requested=False,
            guild_id=1,
        )
        reminder, reminder_permissions = select_eligible_tools(
            request_text="Remind me tomorrow to send the report",
            search_requested=False,
            guild_id=1,
        )

        self.assertNotIn("send_msg", ordinary)
        self.assertNotIn("reminder", ordinary)
        self.assertFalse(permissions.allow_cross_channel_send)
        self.assertIn("reminder", reminder)
        self.assertTrue(reminder_permissions.allow_reminders)

    def test_all_read_tools_are_available_before_search(self) -> None:
        selected, _ = select_eligible_tools(
            request_text="Find the latest earnings",
            search_requested=True,
            guild_id=1,
        )
        expanded = expand_tools_from_outcomes(
            selected,
            [
                ToolOutcome(
                    call_id="1",
                    tool_name="web",
                    arguments="{}",
                    status=ToolStatus.OK,
                    content="Source: https://investor.example.com/results",
                )
            ],
        )

        self.assertEqual(set(READ_ONLY_TOOL_NAMES), selected)
        self.assertIn("url_extract", expanded)
        self.assertIn("browser_extract", expanded)

    def test_current_market_request_exposes_read_tools_without_regex_forcing(self) -> None:
        selected, _ = select_eligible_tools(
            request_text="How is SpaceX stock doing?",
            search_requested=False,
            guild_id=1,
        )

        self.assertEqual(set(READ_ONLY_TOOL_NAMES), selected)
        self.assertEqual(
            set(),
            required_tools_for_request(
                request_text="How is SpaceX stock doing?",
                search_requested=False,
            ),
        )

    def test_current_market_phrasing_does_not_add_manual_required_tools(self) -> None:
        requests = (
            "how did spacex do today",
            "how is spcx doing",
            "what is the valuation of spacex and tesla combined",
            "did that company ipo?",
            "is starlink public yet",
            "what does valuation mean",
            "how did you do that",
            "what is stock based comp",
        )

        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(
                    set(),
                    required_tools_for_request(request_text=request, search_requested=False),
                )

    def test_explicit_search_still_requires_web(self) -> None:
        self.assertEqual(
            {"web"},
            required_tools_for_request(
                request_text="Use search to check $SPCX",
                search_requested=True,
            ),
        )

    def test_annual_dividend_comparison_exposes_all_read_tools(self) -> None:
        request = "Give me dividend and underlying change percentage by year for JEPI. Compare it with SPX."

        selected, _ = select_eligible_tools(
            request_text=request,
            search_requested=False,
            guild_id=1,
        )

        self.assertEqual(set(READ_ONLY_TOOL_NAMES), selected)
        self.assertEqual(set(), required_tools_for_request(request_text=request, search_requested=False))


class ChatOrchestratorBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_uses_one_model_turn(self) -> None:
        orchestrator, llm, tools = _build_orchestrator([_turn(text="Direct answer.")])

        text, _ = await _run(orchestrator)

        self.assertEqual("Direct answer.", text)
        self.assertEqual(["chat_reply"], _features(llm))
        self.assertEqual([], tools.calls)
        exposed = {
            tool["function"]["name"]
            for tool in llm.calls[0]["tools"]
            if isinstance(tool.get("function"), dict)
        }
        self.assertEqual(set(READ_ONLY_TOOL_NAMES), exposed)

    async def test_tool_result_returns_to_same_main_loop(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Grounded answer."),
            ]
        )

        text, _ = await _run(orchestrator, search_requested=True)

        self.assertEqual("Grounded answer.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual(["latest earnings"], tools.queries())

    async def test_per_run_tool_runner_override_is_used(self) -> None:
        orchestrator, _, default_tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "channel_ctx", '{"mode":"raw"}')]),
                _turn(text="Context summary."),
            ]
        )
        benchmark_tools = _FakeToolRunner()

        text, _ = await _run(
            orchestrator,
            request_text="Summarize older channel context.",
            guild_id=1,
            tool_runner=benchmark_tools,
        )

        self.assertEqual("Context summary.", text)
        self.assertEqual([], default_tools.calls)
        self.assertEqual(["channel_ctx"], [call.name for call in benchmark_tools.calls])

    async def test_explicit_ticker_can_use_quote_when_model_chooses_it(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "quote", '{"symbol":"SPCX"}')]),
                _turn(text="SPCX is the current SpaceX listing."),
            ]
        )

        text, _ = await _run(orchestrator, request_text="Check $SPCX ticker")

        self.assertEqual("SPCX is the current SpaceX listing.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual(["quote"], [call.name for call in tools.calls])

    async def test_current_price_answer_with_discovered_ticker_requires_quote_before_final(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"SpaceX current stock ticker price"}')]),
                _turn(text="SpaceX appears to trade as SPCX around $201.80."),
                _turn(tool_calls=[_call("call_2", "quote", '{"symbol":"SPCX"}')]),
                _turn(text="SPCX last traded at $201.80 from the quote tool."),
            ]
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(
            orchestrator,
            request_text="What's the current price of SpaceX?",
            metrics=metrics,
        )

        self.assertEqual("SPCX last traded at $201.80 from the quote tool.", text)
        self.assertEqual(["web", "quote"], [call.name for call in tools.calls])
        self.assertEqual(1, metrics["quote_verification_correction_count"])

    async def test_dividend_history_can_use_annual_performance(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(
                    tool_calls=[
                        _call(
                            "call_1",
                            "annual_perf",
                            '{"symbols":["JEPI","SPX"],"start_year":2020}',
                        )
                    ]
                ),
                _turn(text="Grounded JEPI comparison."),
            ]
        )

        text, _ = await _run(
            orchestrator,
            request_text="Give me divident and underlying change percentage by year for JEPI. Compare it with SPX.",
        )

        self.assertEqual("Grounded JEPI comparison.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual(["annual_perf"], [call.name for call in tools.calls])

    async def test_dividend_history_extracts_source_before_final_answer(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(
                    tool_calls=[
                        _call(
                            "call_1",
                            "annual_perf",
                            '{"symbols":["JEPI","SPX"],"start_year":2020}',
                        )
                    ]
                ),
                _turn(tool_calls=[_call("call_2", "web", '{"query":"JEPI annual returns"}')]),
                _turn(
                    tool_calls=[
                        _call(
                            "call_3",
                            "url_extract",
                            '{"url":"https://example.com/jepi-annual-report","query":"annual distributions"}',
                        )
                    ]
                ),
                _turn(text="Source-grounded comparison."),
            ]
        )
        tools = _SourceToolRunner()

        text, _ = await _run(
            orchestrator,
            request_text="Give me dividend and underlying change percentage by year for JEPI versus SPX.",
            search_requested=True,
            tool_runner=tools,
        )

        self.assertEqual("Source-grounded comparison.", text)
        self.assertEqual(["annual_perf", "web", "url_extract"], [call.name for call in tools.calls])
        self.assertEqual(["chat_reply", "chat_reply", "chat_reply", "chat_reply"], _features(llm))

    async def test_materially_different_followup_search_is_allowed(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"NVIDIA earnings"}')]),
                _turn(tool_calls=[_call("call_2", "web", '{"query":"AMD earnings"}')]),
                _turn(text="Comparison."),
            ]
        )

        text, _ = await _run(orchestrator, search_requested=True)

        self.assertEqual("Comparison.", text)
        self.assertEqual(["NVIDIA earnings", "AMD earnings"], tools.queries())
        self.assertEqual(["chat_reply", "chat_reply", "chat_reply"], _features(llm))

    async def test_multiple_tools_from_one_turn_execute_together(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(
                    tool_calls=[
                        _call("call_1", "web", '{"query":"latest NVDA news"}'),
                        _call("call_2", "quote", '{"symbol":"NVDA"}'),
                    ]
                ),
                _turn(text="Combined answer."),
            ]
        )

        text, _ = await _run(
            orchestrator,
            search_requested=True,
            request_text="latest NVDA stock price and news",
        )

        self.assertEqual("Combined answer.", text)
        self.assertEqual(["web", "quote"], [call.name for call in tools.calls])
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))

    async def test_exact_duplicate_is_skipped_then_model_answers(self) -> None:
        repeated = '{"query":"NVIDIA earnings"}'
        orchestrator, _llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", repeated)]),
                _turn(tool_calls=[_call("call_2", "web", repeated)]),
                _turn(text="Answer from the first result."),
            ]
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, search_requested=True, metrics=metrics)

        self.assertEqual("Answer from the first result.", text)
        self.assertEqual(1, len(tools.calls))
        self.assertEqual(1, metrics["duplicate_tool_call_count"])

    async def test_empty_turn_gets_one_corrective_retry(self) -> None:
        orchestrator, llm, _ = _build_orchestrator([_turn(), _turn(text="Recovered.")])

        text, _ = await _run(orchestrator)

        self.assertEqual("Recovered.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))

    async def test_malformed_tool_call_returns_structured_error_then_model_answers(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", "{not-json")]),
                _turn(text="I could not run that malformed search."),
            ]
        )
        malformed_runner = _MalformedToolRunner()
        orchestrator.tool_runner = malformed_runner

        text, _ = await _run(orchestrator, search_requested=True)

        self.assertEqual("I could not run that malformed search.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual(ToolStatus.ERROR, malformed_runner.outcomes[0].status)
        followup_messages = llm.calls[1]["messages"]
        self.assertIn(
            "missing or invalid",
            str(followup_messages[-1]["content"]),
        )

    async def test_provider_failure_gets_one_tools_disabled_final_call(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [
                RuntimeError("provider unavailable"),
                _turn(text="Recovered without tools."),
            ]
        )
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertEqual("Recovered without tools.", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertIsNone(llm.calls[-1]["tools"])
        self.assertEqual(1, metrics["agent_provider_error_count"])
        self.assertEqual("error", writer.runs[0].step_records[0].status)
        self.assertEqual("provider_error", writer.runs[0].step_records[-1].stop_reason)

    async def test_unexposed_action_tool_is_rejected_without_execution(self) -> None:
        orchestrator, _llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "send_msg", '{"channel":"general","message":"hi"}')]),
                _turn(text="I did not send anything."),
            ]
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertEqual("I did not send anything.", text)
        self.assertEqual([], tools.calls)
        self.assertEqual(1, metrics["unauthorized_tool_call_count"])

    async def test_budget_exhaustion_gets_one_tools_disabled_final_call(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"one"}')]),
                _turn(text="Final from existing evidence."),
            ],
            budget=AgentBudget(max_model_turns=1, max_tool_calls=4),
        )

        text, _ = await _run(orchestrator, search_requested=True)

        self.assertEqual("Final from existing evidence.", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertIsNone(llm.calls[-1]["tools"])
        self.assertEqual(1, len(tools.calls))

    async def test_empty_final_pass_records_failure_reason(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"one"}')]),
                _turn(),
            ],
            budget=AgentBudget(max_model_turns=1, max_tool_calls=4),
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, search_requested=True, metrics=metrics)

        self.assertIn("Result for", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertEqual(1, metrics["chat_empty_final_count"])
        self.assertEqual(1, metrics["chat_final_failure_count"])
        self.assertEqual("empty", metrics["chat_final_failure_reason"])

    async def test_provider_error_final_pass_records_failure_reason(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"one"}')]),
                RuntimeError("provider unavailable"),
            ],
            budget=AgentBudget(max_model_turns=1, max_tool_calls=4),
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, search_requested=True, metrics=metrics)

        self.assertIn("Result for", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertEqual(1, metrics["chat_final_failure_count"])
        self.assertEqual("provider_error", metrics["chat_final_failure_reason"])
        self.assertIn("RuntimeError: provider unavailable", str(metrics["chat_final_failure_error"]))

    async def test_fast_search_is_a_one_tool_budget_then_final_call(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Fast grounded answer."),
            ]
        )

        text, _ = await _run(
            orchestrator,
            search_requested=True,
            fast_search_requested=True,
        )

        self.assertEqual("Fast grounded answer.", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertIsNone(llm.calls[-1]["tools"])
        self.assertEqual(1, len(tools.calls))

    async def test_length_limited_answer_continues_at_most_once(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(text="First half", finish_reason="length"),
                _turn(text="second half", finish_reason="length"),
            ]
        )

        text, _ = await _run(orchestrator)

        self.assertEqual("First half\nsecond half", text)
        self.assertEqual(["chat_reply", "chat_reply_continuation"], _features(llm))

    async def test_reply_final_and_continuation_use_separate_output_budgets(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(text="First half", finish_reason="length"),
                _turn(text="second half"),
            ]
        )
        orchestrator.settings.max_completion_tokens = 1200

        await _run(orchestrator)

        self.assertEqual([1200, 700], [call["max_tokens"] for call in llm.calls])

    async def test_post_tool_answer_turn_has_more_output_headroom(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Grounded answer."),
            ]
        )

        await _run(orchestrator, search_requested=True)

        self.assertEqual([700, 1400], [call["max_tokens"] for call in llm.calls])

    async def test_run_telemetry_is_correlated_and_flushed_once(self) -> None:
        orchestrator, _llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Grounded answer."),
            ]
        )
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer

        await _run(orchestrator, search_requested=True)

        self.assertEqual(1, len(writer.runs))
        run = writer.runs[0]
        self.assertTrue(run.run_id)
        self.assertEqual(
            ["model", "tools", "model", "done"],
            [str(record.state) for record in run.step_records],
        )
        self.assertEqual([1, 2, 3, 4], [record.step_index for record in run.step_records])
        self.assertEqual("web", run.step_records[1].tool_name)
        self.assertEqual(64, len(run.step_records[1].argument_hash))
        self.assertEqual("final_text", run.step_records[-1].stop_reason)

    async def test_deadline_stops_work_and_records_deadline_reason(self) -> None:
        orchestrator, _llm, _tools = _build_orchestrator(
            [],
            budget=AgentBudget(
                max_model_turns=2,
                total_timeout_seconds=0.02,
                finalization_reserve_seconds=0.005,
            ),
        )
        orchestrator.llm_client = _SlowLLM(delay_seconds=0.05)
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer

        text, _ = await _run(orchestrator)

        self.assertIn("couldn't generate a clean reply", text)
        self.assertEqual("deadline", writer.runs[0].step_records[-1].stop_reason)

    async def test_model_request_timeout_is_capped_below_agent_budget(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [_turn(text="simple answer")],
            budget=AgentBudget(total_timeout_seconds=45, finalization_reserve_seconds=8),
        )

        text, _ = await _run(orchestrator)

        self.assertEqual("simple answer", text)
        self.assertEqual(15.0, llm.calls[0]["request_timeout_seconds"])
        self.assertEqual(0, llm.calls[0]["request_max_retries"])


class ToolRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_execution_preserves_partial_success(self) -> None:
        runner = ToolRunner(_MixedExecutor())

        outcomes = await runner.run(
            [_call("ok", "web", "{}"), _call("bad", "quote", "{}")],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertEqual([ToolStatus.OK, ToolStatus.ERROR], [outcome.status for outcome in outcomes])
        self.assertIn("RuntimeError", outcomes[1].content)

    async def test_tool_outcome_carries_latency_metrics_and_provenance(self) -> None:
        runner = ToolRunner(_ProvenanceExecutor())

        outcomes = await runner.run(
            [_call("one", "web", '{"query":"earnings"}')],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertGreaterEqual(outcomes[0].latency_ms, 0)
        self.assertEqual(("https://investor.example.com/results",), outcomes[0].provenance)
        self.assertEqual({"web_search_ms": 3}, outcomes[0].metrics)

    async def test_empty_extract_uses_registry_fallback_guidance(self) -> None:
        runner = ToolRunner(_EmptyExtractExecutor())

        outcomes = await runner.run(
            [_call("one", "url_extract", '{"url":"https://example.com/guessed"}')],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.EMPTY, outcomes[0].status)
        self.assertIn("use web search to locate the exact source URL", outcomes[0].content)


class _FakeLLM:
    def __init__(self, turns: list[object]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, object]] = []

    async def complete_chat_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if not self.turns:
            raise AssertionError("Unexpected extra model call")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        turn.usage.feature = kwargs["feature"]
        return turn


class _SlowLLM:
    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    async def complete_chat_turn(self, **_kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self.delay_seconds)
        return _turn(text="too late")


class _FakeToolRunner:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def run(self, tool_calls, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.extend(tool_calls)
        return [
            ToolOutcome(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolStatus.OK,
                content=f"Result for {call.arguments}",
                metrics={"web_search_ms": 1},
            )
            for call in tool_calls
        ]

    def queries(self) -> list[str]:
        return [json.loads(call.arguments)["query"] for call in self.calls]


class _MalformedToolRunner:
    def __init__(self) -> None:
        self.outcomes: list[ToolOutcome] = []

    async def run(self, tool_calls, **_kwargs):  # type: ignore[no-untyped-def]
        self.outcomes = [
            ToolOutcome(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolStatus.ERROR,
                content="Tool call failed because the query argument was missing or invalid.",
            )
            for call in tool_calls
        ]
        return self.outcomes


class _SourceToolRunner:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def run(self, tool_calls, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.extend(tool_calls)
        return [
            ToolOutcome(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolStatus.OK,
                content=(
                    "Annual report: https://example.com/jepi-annual-report"
                    if call.name == "web"
                    else "Extracted exact annual distribution and NAV data."
                ),
            )
            for call in tool_calls
        ]


class _FakeTelemetryWriter:
    def __init__(self) -> None:
        self.runs: list[AgentRun] = []

    async def flush(self, run: AgentRun, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.runs.append(run)


class _MixedExecutor:
    async def execute(self, *, tool_name: str, **_kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        if tool_name == "quote":
            raise RuntimeError("provider down")
        return "Useful result.", {"web_search_ms": 1}


class _EmptyExtractExecutor:
    async def execute(self, **_kwargs):  # type: ignore[no-untyped-def]
        return "No extractable content found for: https://example.com/guessed", {}


class _ProvenanceExecutor:
    async def execute(self, **_kwargs):  # type: ignore[no-untyped-def]
        return "Official result: https://investor.example.com/results", {"web_search_ms": 3}


def _build_orchestrator(
    turns: list[object],
    *,
    budget: AgentBudget | None = None,
) -> tuple[ChatOrchestrator, _FakeLLM, _FakeToolRunner]:
    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator.settings = SimpleNamespace(max_completion_tokens=700)
    llm = _FakeLLM(turns)
    tools = _FakeToolRunner()
    orchestrator.llm_client = llm
    orchestrator.tool_runner = tools
    orchestrator.agent_budget = budget or AgentBudget()
    return orchestrator, llm, tools


async def _run(
    orchestrator: ChatOrchestrator,
    *,
    search_requested: bool = False,
    metrics: dict[str, int | str] | None = None,
    request_text: str = "Request",
    fast_search_requested: bool = False,
    guild_id: int | None = None,
    tool_runner: ToolRunner | None = None,
) -> tuple[str, list[str]]:
    return await orchestrator.run_chat_with_tools(
        chat_model="chat-model",
        messages=[{"role": "user", "content": "Request"}],
        guild_id=guild_id,
        channel_id=None,
        user_id=1,
        source_message_id=None,
        request_text=request_text,
        search_requested=search_requested,
        fast_search_requested=fast_search_requested,
        metrics=metrics,
        tool_runner=tool_runner,
    )


def _call(call_id: str, name: str, arguments: str) -> object:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def _turn(
    *,
    text: str = "",
    tool_calls: list[object] | None = None,
    finish_reason: str = "stop",
) -> object:
    return SimpleNamespace(
        text=text,
        raw_text=text,
        usage=SimpleNamespace(
            feature="chat_reply",
            model="test-model",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            estimated_cost_usd=0.0,
        ),
        tool_calls=tool_calls or [],
        reasoning_content="",
        finish_reason=finish_reason,
        native_tool_calling_failed=False,
        native_tool_failure_request_json="",
    )


def _features(llm: _FakeLLM) -> list[str]:
    return [str(call["feature"]) for call in llm.calls]


if __name__ == "__main__":
    unittest.main()
