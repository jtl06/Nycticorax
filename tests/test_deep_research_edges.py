from __future__ import annotations

import asyncio
import json
import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.deep_research import (
    CompositeDeepResearchService,
    DeepResearchConfig,
    DeepResearchExtractCall,
    DeepResearchModelCall,
    DeepResearchResult,
    DeepResearchSearchCall,
    MAX_REDUCTION_INPUT_CHARS,
)
from nycti.chat.deep_research_integration import (
    MAX_RESEARCH_CONTEXT_CHARS,
    attach_composite_deep_research,
    should_run_composite_deep_research,
)
from nycti.chat.run_state import (
    AgentBudget,
    AgentPermissions,
    AgentRun,
    AnswerPlan,
    AnswerProfile,
    ToolOutcome,
    ToolStatus,
)
from nycti.chat.search_policy import web_search_options_for_query
from nycti.chat.tool_eligibility import select_answer_plan
from nycti.llm.types import LLMResult, LLMUsage
from nycti.tavily.models import (
    TavilyExtractResponse,
    TavilyExtractResult,
    TavilySearchResponse,
    TavilySearchResult,
)


def _usage(
    *,
    feature: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> LLMUsage:
    return LLMUsage(
        feature=feature,
        model="economy-model",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        estimated_cost_usd=0.00001,
    )


def _deep_run(*, permissions: AgentPermissions | None = None) -> AgentRun:
    budget = AgentBudget(total_timeout_seconds=60, finalization_reserve_seconds=10)
    return AgentRun(
        messages=[],
        budget=budget,
        permissions=permissions or AgentPermissions(),
        answer_plan=AnswerPlan(
            profile=AnswerProfile.DEEP,
            eligible_tool_names=frozenset(),
            budget=budget,
        ),
    )


def _run_for_profile(profile: AnswerProfile, text: str) -> AgentRun:
    plan, permissions = select_answer_plan(
        request_text=text,
        guild_id=None,
        depth_override=profile,
    )
    return AgentRun(
        messages=[],
        budget=plan.budget,
        permissions=permissions,
        answer_plan=plan,
    )


class CompositeResearchCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_research_cancels_all_parallel_searches(self) -> None:
        class ImmediatePlanner:
            async def complete_chat(self, **kwargs: object) -> LLMResult:
                feature = str(kwargs["feature"])
                if feature != "deep_research_plan":
                    raise AssertionError("reduction must not start after cancellation")
                return LLMResult(
                    text='{"queries":["source one","source two"]}',
                    usage=_usage(feature=feature),
                )

        class BlockingSearch:
            def __init__(self) -> None:
                self.started = 0
                self.cancelled = 0
                self.all_started = asyncio.Event()
                self.extract_calls = 0

            async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
                self.started += 1
                if self.started == 2:
                    self.all_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
                raise AssertionError("unreachable")

            async def extract(self, url: str, **kwargs: object) -> object:
                self.extract_calls += 1
                raise AssertionError("extract must not run after cancellation")

        tavily = BlockingSearch()
        service = CompositeDeepResearchService(
            llm_client=ImmediatePlanner(),  # type: ignore[arg-type]
            tavily_client=tavily,  # type: ignore[arg-type]
            config=DeepResearchConfig(economy_model="economy-model"),
        )
        task = asyncio.create_task(service.research("Research a disputed claim."))
        await asyncio.wait_for(tavily.all_started.wait(), timeout=1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        self.assertEqual(2, tavily.started)
        self.assertEqual(2, tavily.cancelled)
        self.assertEqual(0, tavily.extract_calls)

    async def test_cancelling_attachment_is_not_converted_to_research_failure(self) -> None:
        class BlockingService:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.cancelled = False

            async def research(
                self,
                question: str,
                *,
                timeout_seconds: float | None = None,
            ) -> DeepResearchResult:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                raise AssertionError("unreachable")

        service = BlockingService()
        metrics: dict[str, int | str] = {"tool_call_count": 3}
        task = asyncio.create_task(
            attach_composite_deep_research(
                service,  # type: ignore[arg-type]
                _deep_run(),
                "Research the latest evidence with sources.",
                metrics,
                AgentTrace(enabled=True),
            )
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(service.cancelled)
        self.assertEqual({"tool_call_count": 3}, metrics)


class CompositeResearchTriggerPolicyTests(unittest.TestCase):
    def test_specialized_and_stable_requests_stay_on_normal_tool_path(self) -> None:
        url_request = "Review the claims at https://example.com/report."

        self.assertFalse(
            should_run_composite_deep_research(
                _run_for_profile(AnswerProfile.DEEP, url_request),
                url_request,
            )
        )
        self.assertFalse(
            should_run_composite_deep_research(
                _run_for_profile(AnswerProfile.GROUNDED, url_request),
                url_request,
            )
        )
        stable_request = "Explain recursion."
        self.assertFalse(
            should_run_composite_deep_research(
                _run_for_profile(AnswerProfile.DEEP, stable_request),
                stable_request,
            )
        )
        specialized_requests = (
            "Rigorously compare current NVDA and AMD stock prices with sources.",
            "Deeply analyze this YouTube transcript with current sources.",
            "Research the latest inputs and calculate the exact percentage change.",
            "Rigorously verify that with sources.",
            "Check rigorously whether this policy is stale as of July 2026.",
        )
        for request in specialized_requests:
            with self.subTest(request=request):
                self.assertFalse(
                    should_run_composite_deep_research(
                        _run_for_profile(AnswerProfile.DEEP, request),
                        request,
                    )
                )

    def test_stale_or_outdated_freshness_checks_trigger_deep_research(self) -> None:
        request = (
            "Check rigorously whether Illinois biometric privacy guidance is stale or "
            "outdated as of July 2026."
        )

        self.assertTrue(
            should_run_composite_deep_research(
                _run_for_profile(AnswerProfile.DEEP, request),
                request,
            )
        )

    def test_action_permissions_suppress_composite_research(self) -> None:
        cases = (
            "Rigorously research the latest release and remind me tomorrow.",
            "Rigorously research the latest release and post it to the alerts channel.",
        )
        for request in cases:
            with self.subTest(request=request):
                plan, permissions = select_answer_plan(
                    request_text=request,
                    guild_id=123,
                    depth_override=AnswerProfile.DEEP,
                )
                run = AgentRun(
                    messages=[],
                    budget=plan.budget,
                    permissions=permissions,
                    answer_plan=plan,
                )

                self.assertFalse(should_run_composite_deep_research(run, request))

    def test_research_search_policy_preserves_finance_freshness_and_history(self) -> None:
        current = web_search_options_for_query(
            "NVDA latest stock earnings",
            configured_depth="ultra-fast",
        )
        historical = web_search_options_for_query(
            "NVDA stock earnings as of 2024",
            configured_depth="ultra-fast",
        )

        self.assertEqual(
            {"search_depth": "basic", "topic": "finance", "time_range": "week"},
            current,
        )
        self.assertEqual("finance", historical["topic"])
        self.assertIsNone(historical["time_range"])


class CompositeResearchContextAndTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_reducer_message_contains_complete_parseable_evidence_json(self) -> None:
        class CapturingLLM:
            def __init__(self) -> None:
                self.reduction_messages: list[str] = []

            async def complete_chat(self, **kwargs: object) -> LLMResult:
                feature = str(kwargs["feature"])
                if feature == "deep_research_plan":
                    long_query = "Q" * 240
                    return LLMResult(
                        text=json.dumps({"queries": [long_query, "independent countercheck"]}),
                        usage=_usage(feature=feature),
                    )
                messages = kwargs["messages"]
                assert isinstance(messages, list)
                user_message = messages[-1]
                assert isinstance(user_message, dict)
                self.reduction_messages.append(str(user_message["content"]))
                return LLMResult(
                    text='{"summary":"Supported by the supplied evidence [S1].",'
                    '"source_ids":["S1"]}',
                    usage=_usage(feature=feature),
                )

        class LargeEvidenceTavily:
            async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
                query_slug = "long" if query.startswith("Q") else "countercheck"
                return TavilySearchResponse(
                    query=query,
                    results=[
                        TavilySearchResult(
                            title="T" * 300,
                            url=f"https://agency.gov/reports/{query_slug}/{index}",
                            content="S" * 900,
                            score=0.9,
                            published_date="D" * 64,
                        )
                        for index in range(4)
                    ],
                )

            async def extract(
                self,
                url: str,
                **kwargs: object,
            ) -> TavilyExtractResponse:
                return TavilyExtractResponse(
                    url=url,
                    query=str(kwargs.get("query") or ""),
                    results=[
                        TavilyExtractResult(
                            url=url,
                            title="Official source",
                            raw_content="E" * 1_800,
                        )
                    ],
                )

        llm = CapturingLLM()
        service = CompositeDeepResearchService(
            llm_client=llm,  # type: ignore[arg-type]
            tavily_client=LargeEvidenceTavily(),  # type: ignore[arg-type]
            config=DeepResearchConfig(
                economy_model="economy-model",
                max_extracts=8,
            ),
        )

        result = await service.research("Compare the complete evidence rigorously.")

        self.assertTrue(result.has_evidence)
        self.assertEqual(2, len(llm.reduction_messages))
        for outcome in result.outcomes:
            if outcome.status == ToolStatus.OK:
                self.assertTrue(outcome.provenance)
                self.assertTrue(all(url in outcome.content for url in outcome.provenance))
        marker = "Evidence JSON:\n"
        for message in llm.reduction_messages:
            with self.subTest(message_length=len(message)):
                self.assertLessEqual(len(message), MAX_REDUCTION_INPUT_CHARS)
                self.assertIn(marker, message)
                evidence = json.loads(message.split(marker, 1)[1])
                self.assertIsInstance(evidence, list)
                self.assertGreaterEqual(len(evidence), 1)

    async def test_title_only_search_hits_do_not_suppress_normal_fallback(self) -> None:
        class PlannerOnlyLLM:
            async def complete_chat(self, **kwargs: object) -> LLMResult:
                feature = str(kwargs["feature"])
                if feature != "deep_research_plan":
                    raise AssertionError("empty evidence must not be reduced")
                return LLMResult(
                    text='{"queries":["empty source one","empty source two"]}',
                    usage=_usage(feature=feature),
                )

        class TitleOnlyTavily:
            search_depth = "ultra-fast"

            async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
                return TavilySearchResponse(
                    query=query,
                    results=[
                        TavilySearchResult(
                            title="A title without evidence",
                            url=f"https://example.com/{query.replace(' ', '-')}",
                            content="",
                        )
                    ],
                )

            async def extract(self, url: str, **kwargs: object) -> TavilyExtractResponse:
                return TavilyExtractResponse(url=url, query=None, results=[])

        service = CompositeDeepResearchService(
            llm_client=PlannerOnlyLLM(),  # type: ignore[arg-type]
            tavily_client=TitleOnlyTavily(),  # type: ignore[arg-type]
            config=DeepResearchConfig(economy_model="economy-model"),
        )

        result = await service.research("Verify the latest claim with sources.")

        self.assertFalse(result.has_evidence)
        self.assertTrue(all(outcome.status == ToolStatus.EMPTY for outcome in result.outcomes))
        self.assertEqual(1, len(result.model_calls))

    async def test_attached_context_is_hard_bounded_and_provenance_is_retained(self) -> None:
        url = "https://agency.gov/reports/alpha"
        outcome = ToolOutcome(
            call_id="deep-research-1",
            tool_name="deep_research",
            arguments='{"query":"alpha"}',
            status=ToolStatus.OK,
            content="A" * (MAX_RESEARCH_CONTEXT_CHARS * 2),
            provenance=(url,),
        )
        result = DeepResearchResult(
            question="Research alpha",
            queries=("alpha", "alpha countercheck"),
            outcomes=(outcome,),
            model_calls=(),
            search_calls=(),
            extract_calls=(),
            used_fallback=False,
        )
        run = _deep_run()

        attached = await attach_composite_deep_research(
            _StaticService(result),  # type: ignore[arg-type]
            run,
            "Research the latest Alpha report with sources.",
            {},
            AgentTrace(enabled=False),
        )

        self.assertTrue(attached)
        research_context = str(run.messages[0]["content"])
        self.assertLessEqual(len(research_context), MAX_RESEARCH_CONTEXT_CHARS)
        self.assertIn("[truncated]", research_context)
        self.assertEqual((url,), run.outcomes[0].provenance)
        self.assertIn(url, str(run.messages[1]["content"]))

    async def test_existing_telemetry_counts_are_incremented_not_overwritten(self) -> None:
        result = _telemetry_result()
        run = _deep_run()
        metrics: dict[str, int | str] = {
            "tool_call_count": 7,
            "web_search_query_count": 5,
            "url_extract_count": 4,
            "web_search_ms": 11,
            "url_extract_ms": 13,
            "chat_llm_ms": 17,
            "chat_prompt_tokens": 19,
            "chat_completion_tokens": 23,
            "chat_total_tokens": 42,
        }

        attached = await attach_composite_deep_research(
            _StaticService(result),  # type: ignore[arg-type]
            run,
            "Research the latest result with sources.",
            metrics,
            AgentTrace(enabled=False),
        )

        self.assertTrue(attached)
        self.assertEqual(11, metrics["tool_call_count"])
        self.assertEqual(7, metrics["web_search_query_count"])
        self.assertEqual(6, metrics["url_extract_count"])
        self.assertEqual(20, metrics["web_search_ms"])
        self.assertEqual(20, metrics["url_extract_ms"])
        self.assertEqual(24, metrics["chat_llm_ms"])
        self.assertEqual(32, metrics["chat_prompt_tokens"])
        self.assertEqual(31, metrics["chat_completion_tokens"])
        self.assertEqual(63, metrics["chat_total_tokens"])
        self.assertEqual(4, run.tool_calls)
        self.assertEqual(2, len(run.usage_records))
        self.assertEqual(8, len(run.step_records))
        reduction_record = next(
            record for record in run.step_records if record.feature == "deep_research_reduce"
        )
        self.assertNotIn("query", reduction_record.details)
        self.assertEqual(64, len(str(reduction_record.details["query_hash"])))


class _StaticService:
    def __init__(self, result: DeepResearchResult) -> None:
        self.result = result

    async def research(
        self,
        question: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeepResearchResult:
        return self.result


def _telemetry_result() -> DeepResearchResult:
    outcome = ToolOutcome(
        call_id="deep-research-1",
        tool_name="deep_research",
        arguments=json.dumps({"query": "official result"}),
        status=ToolStatus.OK,
        content="Official evidence supports the result.",
        provenance=("https://agency.gov/result",),
    )
    return DeepResearchResult(
        question="Research the result",
        queries=("official result", "independent result", "late result"),
        outcomes=(outcome,),
        model_calls=(
            DeepResearchModelCall(
                stage="planning",
                feature="deep_research_plan",
                model="economy-model",
                status="ok",
                latency_ms=3,
                usage=_usage(feature="deep_research_plan", prompt_tokens=6, completion_tokens=3),
            ),
            DeepResearchModelCall(
                stage="reduction",
                feature="deep_research_reduce",
                model="economy-model",
                status="ok",
                latency_ms=4,
                usage=_usage(feature="deep_research_reduce", prompt_tokens=7, completion_tokens=5),
                query="official result",
            ),
        ),
        search_calls=(
            DeepResearchSearchCall("official result", "ok", 9, 1),
            DeepResearchSearchCall("independent result", "timeout", 8, 0, "TimeoutError"),
            DeepResearchSearchCall("late result", "skipped", 0, 0, "overall_timeout"),
        ),
        extract_calls=(
            DeepResearchExtractCall("https://agency.gov/result", "ok", 7, 80, False),
            DeepResearchExtractCall("https://other.example/result", "timeout", 6, 0, True),
            DeepResearchExtractCall("https://late.example/result", "skipped", 0, 0, True),
        ),
        used_fallback=True,
    )


if __name__ == "__main__":
    unittest.main()
