from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
import unittest

from nycti.chat.evidence import build_evidence_ledger
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import (
    AgentBudget,
    AgentRun,
    AnswerProfile,
    CorrectionKind,
    StopReason,
    ToolExposure,
    ToolOutcome,
    ToolStatus,
)
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.chat.tool_eligibility import (
    READ_ONLY_TOOL_NAMES,
    expand_tools_from_outcomes,
    select_answer_plan,
    select_eligible_tools,
)
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
        self.assertIn("Memory stocks were particularly hard hit", result)
        self.assertIn("wsj.com", result)
        self.assertNotIn("Tavily web results for:", result)
        self.assertNotIn("Unsynthesized snippets", result)


class AgentRunTests(unittest.TestCase):
    GUILD_TOOL_NAMES = READ_ONLY_TOOL_NAMES | {"reminder", "send_msg"}

    def test_state_and_budget_are_typed(self) -> None:
        run = AgentRun(messages=[], budget=AgentBudget(max_model_turns=2, max_tool_calls=1))
        self.assertTrue(run.can_start_model_turn())
        self.assertEqual(1, run.remaining_tool_calls())
        run.stop_reason = StopReason.FINAL_TEXT
        self.assertEqual("final_text", run.stop_reason)

    def test_corrections_are_one_shot_per_category_and_globally_bounded(self) -> None:
        run = AgentRun(messages=[], budget=AgentBudget(max_corrections=2))

        self.assertTrue(run.use_correction(CorrectionKind.DUPLICATE_TOOL))
        self.assertFalse(run.use_correction(CorrectionKind.DUPLICATE_TOOL))
        self.assertTrue(run.use_correction(CorrectionKind.EVIDENCE_REPAIR))
        self.assertFalse(run.use_correction(CorrectionKind.EMPTY_TURN))
        self.assertEqual(2, run.corrections)
        self.assertEqual(
            {CorrectionKind.DUPLICATE_TOOL, CorrectionKind.EVIDENCE_REPAIR},
            run.correction_kinds,
        )

    def test_action_proposal_tools_do_not_derive_authority_from_request_text(self) -> None:
        ordinary, _ = select_eligible_tools(
            request_text="What is NVIDIA trading at?",
            guild_id=1,
        )
        reminder, _ = select_eligible_tools(
            request_text="Remind me tomorrow to send the report",
            guild_id=1,
        )
        direct_message, _ = select_eligible_tools(
            request_text="Remind me tomorrow to send the report",
            guild_id=None,
        )

        self.assertTrue({"reminder", "send_msg"}.issubset(ordinary))
        self.assertEqual(ordinary, reminder)
        self.assertTrue({"reminder", "send_msg"}.isdisjoint(direct_message))

    def test_answer_plan_defaults_ambiguous_requests_to_existing_grounded_path(self) -> None:
        base_budget = AgentBudget()

        plan, _ = select_answer_plan(
            request_text="Can you help me think about this?",
            guild_id=1,
            default_budget=base_budget,
        )

        self.assertEqual(AnswerProfile.GROUNDED, plan.profile)
        self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
        self.assertIs(base_budget, plan.budget)
        self.assertIsNone(plan.reasoning_effort_override)
        self.assertEqual("ambiguous_default", plan.selection_reason)

    def test_answer_plan_uses_quick_path_only_for_strong_simple_signal(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Tell me a joke",
            guild_id=1,
        )

        self.assertEqual(AnswerProfile.QUICK, plan.profile)
        self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
        self.assertEqual("low", plan.reasoning_effort_override)
        self.assertLess(plan.budget.total_timeout_seconds, AgentBudget().total_timeout_seconds)
        self.assertEqual(AgentBudget().max_tool_calls, plan.budget.max_tool_calls)
        self.assertEqual(AgentBudget().max_corrections, plan.budget.max_corrections)
        self.assertEqual(0, plan.budget.max_continuations)

    def test_prior_freshness_failures_keep_every_safe_read_tool_reachable(self) -> None:
        requests = (
            "What is OpenAI's newest model?",
            "Explain the July 2026 tariff changes.",
            "How does Python 3.15 differ from 3.14?",
            "What is Palworld 1.0?",
            "How does GPT-5.6 differ from GPT-5.5?",
            "What is GPT-5?",
            "How does macOS 27 differ from iOS 27?",
            "What is Android 17?",
            "What is OpenAI's new model?",
            "What is the new iPhone?",
            "What is the next iPhone?",
            "Explain the recently announced tariff changes.",
            "Explain the 2026 tariff changes.",
            "What is the 2026 tax policy?",
            "React 20 vs React 19",
            "Angular 21",
            "Rust 1.90 vs 1.89",
            "TypeScript6.0",
            "Llama5",
            "Grok4",
            "new tariffs",
            "this month's policy changes",
            "Say what OpenAI's newest model is",
            "Do you think this new iPhone is worth it?",
        )

        for request in requests:
            with self.subTest(request=request):
                plan, _ = select_answer_plan(request_text=request, guild_id=1)
                self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
                self.assertFalse(plan.deferred_tool_names)
                self.assertEqual(ToolExposure.DIRECT, plan.exposure_for("web"))

    def test_stable_explanations_keep_read_tools_under_any_budget(self) -> None:
        requests = (
            "Explain recursion.",
            "What is stock based compensation?",
            "Explain the 2008 financial crisis.",
            "Explain RFC 2616 from 1999.",
            "What is 3.14?",
            "Explain section 2.3...",
            "What is iPhone 17?",
            "What is OpenAI o4?",
            "How does o4 differ from o3?",
            "What is Debian 13?",
            "What is Chrome 140?",
            "Explain the tariff changes.",
            "What are the tariffs?",
            "Explain the July tariff changes.",
            "/depth auto What is iPhone 17?",
            "depth: auto What is OpenAI o4?",
            "/depth auto Explain the tariff changes.",
        )

        for request in requests:
            with self.subTest(request=request):
                plan, _ = select_answer_plan(request_text=request, guild_id=1)
                self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)

    def test_number_fragments_do_not_remove_tools(self) -> None:
        requests = (
            "3.14",
            "section 2.3",
            "semantic version 1.0",
        )

        for request in requests:
            with self.subTest(request=request):
                plan, _ = select_answer_plan(request_text=request, guild_id=1)
                self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)

    def test_answer_plan_detects_deep_research_and_preserves_full_tool_bundle(self) -> None:
        plan, _ = select_answer_plan(
            request_text=(
                "Compare the latest NVIDIA and AMD earnings and verify the guidance with source links."
            ),
            guild_id=1,
        )

        self.assertEqual(AnswerProfile.DEEP, plan.profile)
        self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
        self.assertEqual("high", plan.reasoning_effort_override)
        self.assertGreater(plan.budget.total_timeout_seconds, AgentBudget().total_timeout_seconds)

    def test_simple_current_comparison_stays_grounded(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Compare the current prices of AAPL and MSFT.",
            guild_id=1,
        )

        self.assertEqual(AnswerProfile.GROUNDED, plan.profile)
        self.assertIsNone(plan.reasoning_effort_override)

    def test_explicit_depth_override_changes_budget_not_proposal_reachability(self) -> None:
        plan, _ = select_answer_plan(
            request_text="depth: quick remind me tomorrow to file the report",
            guild_id=1,
        )

        self.assertEqual(AnswerProfile.QUICK, plan.profile)
        self.assertTrue(plan.explicit_override)
        self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)

    def test_explicit_auto_uses_conservative_detection(self) -> None:
        plan, _ = select_answer_plan(
            request_text="/depth auto tell me a joke",
            guild_id=1,
        )

        self.assertEqual(AnswerProfile.QUICK, plan.profile)
        self.assertTrue(plan.explicit_override)
        self.assertTrue(plan.selection_reason.startswith("explicit_auto:"))

    def test_textual_depth_override_wins_over_runtime_preference(self) -> None:
        plan, _ = select_answer_plan(
            request_text="depth: quick tell me a joke",
            guild_id=1,
            depth_override=AnswerProfile.DEEP,
        )

        self.assertEqual(AnswerProfile.QUICK, plan.profile)
        self.assertTrue(plan.explicit_override)

    def test_grounded_search_promotes_relevant_tools_without_hiding_others(self) -> None:
        selected, _ = select_eligible_tools(
            request_text="Find the latest earnings",
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

        plan, _ = select_answer_plan(
            request_text="Find the latest earnings",
            guild_id=1,
        )

        self.assertEqual(set(self.GUILD_TOOL_NAMES), selected)
        self.assertEqual(("web", "quote", "url_extract"), plan.promoted_tool_names)
        self.assertIn("url_extract", expanded)
        self.assertIn("browser_extract", expanded)

    def test_current_market_request_promotes_tools_without_forcing(self) -> None:
        selected, _ = select_eligible_tools(
            request_text="How is SpaceX stock doing?",
            guild_id=1,
        )

        plan, _ = select_answer_plan(request_text="How is SpaceX stock doing?", guild_id=1)

        self.assertEqual(set(self.GUILD_TOOL_NAMES), selected)
        self.assertEqual(("quote", "web"), plan.promoted_tool_names)

    def test_request_phrasing_produces_nonbinding_promotion_hints(self) -> None:
        requests = {
            "how did spacex do today": {"quote", "web"},
            "how is spcx doing": {"quote", "web"},
            "what is the valuation of spacex and tesla combined": {
                "quote",
                "url_extract",
                "web",
            },
            "did that company ipo?": {"quote", "url_extract", "web"},
            "is starlink public yet": {"quote", "url_extract", "web"},
            "what does valuation mean": set(),
            "how did you do that": set(),
            "what is stock based comp": set(),
        }

        for request, expected in requests.items():
            with self.subTest(request=request):
                plan, _ = select_answer_plan(request_text=request, guild_id=1)
                self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
                self.assertEqual(expected, set(plan.promoted_tool_names))

    def test_annual_dividend_comparison_promotes_focused_tools(self) -> None:
        request = "Give me dividend and underlying change percentage by year for JEPI. Compare it with SPX."

        plan, _ = select_answer_plan(
            request_text=request,
            guild_id=1,
        )

        self.assertEqual(self.GUILD_TOOL_NAMES, plan.eligible_tool_names)
        self.assertEqual(
            {"annual_perf", "python", "url_extract", "web"},
            set(plan.promoted_tool_names),
        )


class ChatOrchestratorBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_uses_one_model_turn(self) -> None:
        orchestrator, llm, tools = _build_orchestrator([_turn(text="Direct answer.")])
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertEqual("Direct answer.", text)
        self.assertEqual(["chat_reply"], _features(llm))
        self.assertEqual([], tools.calls)
        exposed = {
            tool["function"]["name"]
            for tool in llm.calls[0]["tools"]
            if isinstance(tool.get("function"), dict)
        }
        self.assertEqual(set(READ_ONLY_TOOL_NAMES), exposed)
        self.assertEqual("unknown", metrics["active_chat_provider"])
        self.assertEqual(120, metrics["agent_total_tokens"])
        self.assertIn('"name": "web"', str(metrics["_diagnostic_tool_schemas_json"]))
        self.assertIn('"role": "user"', str(metrics["_diagnostic_agent_messages_json"]))

    async def test_model_cannot_omit_server_validated_action_card(self) -> None:
        card = (
            "Confirmation required\n"
            "Proposal: `act_exact123`\n"
            "Action: send channel message\n"
            "Target: <#123> (`123`)\n"
            'Message: "exact payload"\n'
            "Nothing has been executed.\n"
            "Confirm in this channel with `/confirm proposal:act_exact123`."
        )
        orchestrator, _, _ = _build_orchestrator(
            [
                _turn(
                    tool_calls=[
                        _call(
                            "action-1",
                            "send_msg",
                            '{"channel":"123","message":"exact payload"}',
                        )
                    ]
                ),
                _turn(text="Done — I already sent something else."),
            ]
        )
        action_runner = _ActionProposalToolRunner(card)

        text, _ = await _run(
            orchestrator,
            request_text="Please post this update.",
            guild_id=7,
            tool_runner=action_runner,  # type: ignore[arg-type]
        )

        self.assertIn("Done — I already sent something else.", text)
        self.assertIn("Server-validated pending action", text)
        self.assertIn(card, text)

    async def test_quick_profile_keeps_read_tools_and_overrides_reasoning_effort(self) -> None:
        orchestrator, llm, tools = _build_orchestrator([_turn(text="A short joke.")])
        metrics: dict[str, int | str] = {}

        text, _ = await _run(
            orchestrator,
            request_text="Tell me a joke",
            metrics=metrics,
        )

        self.assertEqual("A short joke.", text)
        exposed = {
            tool["function"]["name"]
            for tool in llm.calls[0]["tools"]
            if isinstance(tool.get("function"), dict)
        }
        self.assertEqual(set(READ_ONLY_TOOL_NAMES), exposed)
        self.assertEqual("low", llm.calls[0]["reasoning_effort_override"])
        self.assertEqual([], tools.calls)
        self.assertEqual("quick", metrics["answer_profile"])
        self.assertEqual(len(READ_ONLY_TOOL_NAMES), metrics["exposed_tool_count"])

    async def test_answer_profiles_can_route_to_dedicated_models(self) -> None:
        quick, quick_llm, _ = _build_orchestrator([_turn(text="Fast answer.")])
        quick.settings.openai_quick_model = "fast-model"
        deep, deep_llm, _ = _build_orchestrator([_turn(text="Rigorous answer.")])
        deep.settings.openai_deep_model = "rigorous-model"

        await _run(quick, request_text="Explain recursion.")
        await _run(
            deep,
            request_text="Do a rigorous deep-dive with multiple independent sources.",
        )

        self.assertEqual("fast-model", quick_llm.calls[0]["model"])
        self.assertEqual("rigorous-model", deep_llm.calls[0]["model"])

    async def test_deep_profile_overrides_reasoning_and_expands_budget(self) -> None:
        orchestrator, llm, _ = _build_orchestrator([_turn(text="Deep answer.")])
        metrics: dict[str, int | str] = {}

        text, _ = await _run(
            orchestrator,
            request_text="Do a rigorous deep-dive with multiple independent sources.",
            metrics=metrics,
        )

        self.assertEqual("Deep answer.", text)
        self.assertEqual("high", llm.calls[0]["reasoning_effort_override"])
        self.assertEqual("deep", metrics["answer_profile"])
        self.assertEqual("60.0", metrics["answer_timeout_seconds"])

    async def test_deep_responses_profile_reserves_space_for_hidden_reasoning(self) -> None:
        orchestrator, llm, _ = _build_orchestrator([_turn(text="Deep answer.")])
        llm.provider_capabilities = SimpleNamespace(name="openai")

        await _run(
            orchestrator,
            request_text="Do a rigorous deep-dive with multiple independent sources.",
            chat_model="gpt-5.6-luna",
        )

        self.assertEqual(4096, llm.calls[0]["max_tokens"])

    async def test_tool_result_returns_to_same_main_loop(self) -> None:
        orchestrator, llm, tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Grounded answer."),
            ]
        )

        text, _ = await _run(orchestrator)

        self.assertEqual("Grounded answer.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual(["latest earnings"], tools.queries())
        self.assertEqual([0.7, 0.4], [call["temperature"] for call in llm.calls])

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

    async def test_quote_verification_does_not_consume_evidence_repair_allowance(self) -> None:
        tool_runner = _EvidenceToolRunner()
        evidence_id = build_evidence_ledger([tool_runner.outcome]).items[0].evidence_id
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"SpaceX ticker"}')]),
                _turn(text="SpaceX appears to trade as SPCX around $201.80."),
                _turn(tool_calls=[_call("call_2", "quote", '{"symbol":"SPCX"}')]),
                _turn(text="SPCX is $201.80. https://invented.example/report"),
                _turn(text=f"SPCX is $201.80. [{evidence_id}]"),
            ]
        )
        metrics: dict[str, int | str] = {}
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer

        text, _ = await _run(
            orchestrator,
            request_text="What's the current price of SpaceX?",
            tool_runner=tool_runner,
            metrics=metrics,
        )

        self.assertNotIn("invented.example", text)
        self.assertIn(evidence_id, text)
        self.assertEqual(["web", "quote"], [call.name for call in tool_runner.calls])
        self.assertEqual(1, metrics["quote_verification_correction_count"])
        self.assertEqual(1, metrics["evidence_repair_count"])
        self.assertEqual(2, metrics["agent_correction_count"])
        self.assertEqual(
            "evidence_repair, quote_verification",
            metrics["agent_correction_categories"],
        )
        self.assertEqual(
            ["chat_reply", "chat_reply", "chat_reply", "chat_reply", "chat_reply"],
            _features(llm),
        )
        self.assertEqual(
            ["evidence_repair", "quote_verification"],
            writer.runs[0].step_records[-1].details["correction_categories"],
        )

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

        text, _ = await _run(orchestrator)

        self.assertEqual("Comparison.", text)
        self.assertEqual(["NVIDIA earnings", "AMD earnings"], tools.queries())
        self.assertEqual(["chat_reply", "chat_reply", "chat_reply"], _features(llm))

    async def test_researched_answer_repairs_citations_and_lists_observed_source(self) -> None:
        tool_runner = _EvidenceToolRunner()
        evidence_id = build_evidence_ledger([tool_runner.outcome]).items[0].evidence_id
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"official results"}')]),
                _turn(text="Claim from https://invented.example/report."),
                _turn(text=f"The official result supports the claim. [{evidence_id}]"),
            ]
        )
        metrics: dict[str, int | str] = {}

        text, _ = await _run(
            orchestrator,
            request_text="Verify the latest official results with sources.",
            tool_runner=tool_runner,
            metrics=metrics,
        )

        self.assertNotIn("invented.example", text)
        self.assertIn(evidence_id, text)
        self.assertIn("Sources:", text)
        self.assertIn("https://example.com/report", text)
        self.assertEqual(1, metrics["evidence_repair_count"])
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

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertEqual("Answer from the first result.", text)
        self.assertEqual(1, len(tools.calls))
        self.assertEqual(1, metrics["duplicate_tool_call_count"])
        self.assertEqual("duplicate_tool", metrics["agent_correction_categories"])

    async def test_empty_turn_gets_one_corrective_retry(self) -> None:
        orchestrator, llm, _ = _build_orchestrator([_turn(), _turn(text="Recovered.")])
        metrics: dict[str, int | str] = {}

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertEqual("Recovered.", text)
        self.assertEqual(["chat_reply", "chat_reply"], _features(llm))
        self.assertEqual("empty_turn", metrics["agent_correction_categories"])

    async def test_malformed_tool_call_returns_structured_error_then_model_answers(self) -> None:
        orchestrator, llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", "{not-json")]),
                _turn(text="I could not run that malformed search."),
            ]
        )
        malformed_runner = _MalformedToolRunner()
        orchestrator.tool_runner = malformed_runner

        text, _ = await _run(orchestrator)

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

        text, _ = await _run(orchestrator)

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

        text, _ = await _run(orchestrator, metrics=metrics)

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

        text, _ = await _run(orchestrator, metrics=metrics)

        self.assertIn("Result for", text)
        self.assertEqual(["chat_reply", "chat_reply_final"], _features(llm))
        self.assertEqual(1, metrics["chat_final_failure_count"])
        self.assertEqual("provider_error", metrics["chat_final_failure_reason"])
        self.assertIn("RuntimeError: provider unavailable", str(metrics["chat_final_failure_error"]))

    async def test_length_limited_answer_continues_at_most_once(self) -> None:
        initial_turn = _turn(text="First half", finish_reason="length")
        initial_turn.response_output_items = [
            {"type": "reasoning", "encrypted_content": "opaque-state"},
            {"type": "message", "content": [{"type": "output_text", "text": "First half"}]},
        ]
        orchestrator, llm, _ = _build_orchestrator(
            [
                initial_turn,
                _turn(text="second half", finish_reason="length"),
            ]
        )

        text, _ = await _run(orchestrator)

        self.assertEqual("First half\nsecond half", text)
        self.assertEqual(["chat_reply", "chat_reply_continuation"], _features(llm))
        continuation_assistant = llm.calls[1]["messages"][-2]
        self.assertEqual(initial_turn.response_output_items, continuation_assistant["responses_output_items"])

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

        await _run(orchestrator)

        self.assertEqual([700, 1400], [call["max_tokens"] for call in llm.calls])

    async def test_quick_profile_caps_each_output_token_budget(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"joke origin"}')]),
                _turn(text="A short answer."),
            ]
        )
        orchestrator.settings.max_completion_tokens = 4096
        metrics: dict[str, int | str] = {}

        await _run(orchestrator, request_text="Tell me a joke", metrics=metrics)

        self.assertEqual([700, 1200], [call["max_tokens"] for call in llm.calls])
        self.assertEqual(700, metrics["answer_reply_token_budget"])
        self.assertEqual(1200, metrics["answer_tool_followup_token_budget"])
        self.assertEqual(1400, metrics["answer_final_token_budget"])
        self.assertEqual(500, metrics["answer_continuation_token_budget"])

    async def test_run_telemetry_is_correlated_and_flushed_once(self) -> None:
        orchestrator, _llm, _tools = _build_orchestrator(
            [
                _turn(tool_calls=[_call("call_1", "web", '{"query":"latest earnings"}')]),
                _turn(text="Grounded answer."),
            ]
        )
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer

        await _run(orchestrator)

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

    async def test_request_arrival_timestamp_starts_agent_deadline(self) -> None:
        orchestrator, llm, _ = _build_orchestrator(
            [_turn(text="Should not be called.")],
            budget=AgentBudget(
                total_timeout_seconds=0.1,
                finalization_reserve_seconds=0,
            ),
        )
        writer = _FakeTelemetryWriter()
        orchestrator.telemetry_writer = writer
        request_started_at = time.perf_counter() - 1

        await _run(
            orchestrator,
            request_started_at=request_started_at,
        )

        self.assertEqual([], llm.calls)
        self.assertEqual(request_started_at, writer.runs[0].started_at)
        self.assertEqual(StopReason.DEADLINE, writer.runs[0].stop_reason)

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


class _ActionProposalToolRunner:
    def __init__(self, card: str) -> None:
        self.card = card

    async def run(self, tool_calls, **_kwargs):  # type: ignore[no-untyped-def]
        return [
            ToolOutcome(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolStatus.OK,
                content=self.card,
                metrics={
                    "action_proposal_count": 1,
                    "action_proposal_kind": "send_channel_message",
                },
            )
            for call in tool_calls
        ]


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


class _EvidenceToolRunner:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.outcome = ToolOutcome(
            call_id="call_1",
            tool_name="web",
            arguments='{"query":"official results"}',
            status=ToolStatus.OK,
            content="The official report supports the claim.",
            provenance=("https://example.com/report",),
        )

    async def run(self, tool_calls, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.extend(tool_calls)
        return [
            ToolOutcome(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolStatus.OK,
                content=self.outcome.content,
                provenance=self.outcome.provenance,
            )
            for call in tool_calls
        ]


class _FakeTelemetryWriter:
    def __init__(self) -> None:
        self.runs: list[AgentRun] = []

    async def flush(self, run: AgentRun, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.runs.append(run)


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
    metrics: dict[str, int | str] | None = None,
    request_text: str = "Request",
    guild_id: int | None = None,
    tool_runner: ToolRunner | None = None,
    chat_model: str = "chat-model",
    request_started_at: float | None = None,
) -> tuple[str, list[str]]:
    return await orchestrator.run_chat_with_tools(
        chat_model=chat_model,
        messages=[{"role": "user", "content": "Request"}],
        guild_id=guild_id,
        channel_id=None,
        user_id=1,
        source_message_id=None,
        request_text=request_text,
        metrics=metrics,
        tool_runner=tool_runner,
        request_started_at=request_started_at,
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
