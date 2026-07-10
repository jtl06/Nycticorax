from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest

from nycti.chat.deep_research import (
    CompositeDeepResearchService,
    DeepResearchConfig,
    DeepResearchExtractCall,
    DeepResearchModelCall,
    DeepResearchResult,
    DeepResearchSearchCall,
)
from nycti.chat.deep_research_integration import build_composite_deep_research_service
from nycti.chat.evidence import build_evidence_ledger
from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import AgentBudget, ToolOutcome, ToolStatus
from nycti.llm.tool_calls import LLMToolCall
from nycti.llm.types import LLMChatTurn, LLMResult, LLMUsage
from nycti.tavily.models import (
    TavilyExtractResponse,
    TavilyExtractResult,
    TavilySearchResponse,
    TavilySearchResult,
)


class CompositeDeepResearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_builder_prefers_configured_fallback_provider_for_research(self) -> None:
        fallback = SimpleNamespace(
            settings=SimpleNamespace(openai_chat_model="deepseek-ai/DeepSeek-V4-Pro")
        )
        primary = SimpleNamespace(fallback_client=fallback)
        settings = SimpleNamespace(openai_memory_model="primary-efficiency-model")
        tavily = SimpleNamespace(api_key="test-key")

        service = build_composite_deep_research_service(
            settings,  # type: ignore[arg-type]
            primary,  # type: ignore[arg-type]
            tavily,  # type: ignore[arg-type]
        )

        self.assertIsNotNone(service)
        assert service is not None
        self.assertIs(service.llm_client, fallback)
        self.assertEqual("deepseek-ai/DeepSeek-V4-Pro", service.config.economy_model)

    async def test_economy_model_plans_and_reduces_parallel_search_evidence(self) -> None:
        queries = (
            "NVIDIA latest earnings official release",
            "AMD latest earnings official release",
            "NVIDIA AMD guidance comparison",
        )
        llm = _EconomyLLM(queries=queries)
        tavily = _ConcurrentTavilyClient()
        service = _service(llm, tavily)

        result = await service.research(
            "Compare NVIDIA and AMD's latest earnings and guidance with primary sources."
        )

        self.assertEqual(queries, result.queries)
        self.assertGreaterEqual(tavily.max_active_searches, 2)
        self.assertEqual(set(queries), set(tavily.searches))
        self.assertTrue(result.has_evidence)
        self.assertEqual("ok", result.status)
        self.assertTrue(result.outcomes)
        self.assertTrue(all(outcome.status == ToolStatus.OK for outcome in result.outcomes))
        self.assertTrue(all(outcome.provenance for outcome in result.outcomes))

        self.assertEqual({"economy-model"}, {str(call["model"]) for call in llm.calls})
        self.assertEqual({"economy-model"}, {call.model for call in result.model_calls})
        self.assertEqual(1, sum(call.stage == "planning" for call in result.model_calls))
        self.assertEqual(len(queries), sum(call.stage == "reduction" for call in result.model_calls))
        self.assertTrue(all(call.usage is not None for call in result.model_calls))
        self.assertEqual(len(result.model_calls), len(result.usages))
        self.assertNotIn("strong-model", {str(call["model"]) for call in llm.calls})

    async def test_provenance_survives_reduction_and_supports_citation_contract(self) -> None:
        llm = _EconomyLLM(queries=("official alpha report", "official beta report"))
        tavily = _ConcurrentTavilyClient()

        result = await _service(llm, tavily).research("Compare alpha and beta rigorously.")
        ledger = build_evidence_ledger(result.outcomes)

        observed_urls = {
            search_result.url
            for response in tavily.responses.values()
            for search_result in response.results
        }
        self.assertTrue(ledger.researched)
        self.assertTrue(set(ledger.provenance_urls).issubset(observed_urls))
        cited = ledger.audit_answer(
            f"The evidence supports the comparison. [{ledger.evidence_ids[0]}]"
        )
        invented = ledger.audit_answer(
            "The evidence supports the comparison: https://invented.example/report"
        )
        self.assertTrue(cited.valid)
        self.assertFalse(invented.valid)
        self.assertEqual(
            ("https://invented.example/report",),
            invented.unprovenanced_urls,
        )

    async def test_partial_search_failure_keeps_successful_evidence(self) -> None:
        queries = ("working source", "broken source", "another working source")
        llm = _EconomyLLM(queries=queries)
        tavily = _ConcurrentTavilyClient(failing_queries={"broken source"})

        result = await _service(llm, tavily).research("Research a disputed claim.")

        self.assertEqual("partial", result.status)
        self.assertTrue(result.has_evidence)
        self.assertEqual(2, sum(call.status == "ok" for call in result.search_calls))
        self.assertEqual(1, sum(call.status == "error" for call in result.search_calls))
        self.assertTrue(any(outcome.status == ToolStatus.OK for outcome in result.outcomes))
        self.assertGreaterEqual(int(result.metrics["deep_research_search_failure_count"]), 1)

    async def test_search_timeout_is_bounded_and_reported_as_partial(self) -> None:
        queries = ("fast source one", "slow source", "fast source two")
        llm = _EconomyLLM(queries=queries)
        tavily = _ConcurrentTavilyClient(slow_queries={"slow source"})
        service = _service(
            llm,
            tavily,
            search_timeout_seconds=0.01,
            overall_timeout_seconds=1.0,
        )

        result = await service.research("Research this with a bounded deadline.")

        self.assertEqual("partial", result.status)
        self.assertTrue(result.has_evidence)
        slow_call = next(call for call in result.search_calls if call.query == "slow source")
        self.assertEqual("timeout", slow_call.status)
        self.assertLess(slow_call.latency_ms, 250)

    async def test_invalid_plan_uses_bounded_deterministic_query_fallback(self) -> None:
        llm = _EconomyLLM(queries=(), planning_text="not valid JSON")
        tavily = _ConcurrentTavilyClient()

        result = await _service(llm, tavily).research(
            "Compare the latest quarterly performance of Alpha and Beta."
        )

        self.assertTrue(result.used_fallback)
        self.assertGreaterEqual(len(result.queries), 2)
        self.assertLessEqual(len(result.queries), 4)
        self.assertEqual(len(result.queries), len(set(result.queries)))
        self.assertTrue(all(query.strip() for query in result.queries))
        self.assertEqual("economy-model", llm.calls[0]["model"])

    async def test_all_search_failures_skip_reduction_and_return_no_evidence(self) -> None:
        queries = ("broken source one", "broken source two")
        llm = _EconomyLLM(queries=queries)
        tavily = _ConcurrentTavilyClient(failing_queries=set(queries))

        result = await _service(llm, tavily).research("Research unavailable evidence.")

        self.assertFalse(result.has_evidence)
        self.assertIn(result.status, {"empty", "error"})
        self.assertFalse(any(call.stage == "reduction" for call in result.model_calls))
        self.assertFalse(result.usages[1:])
        self.assertTrue(all(outcome.status != ToolStatus.OK for outcome in result.outcomes))


class CompositeResearchOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_eligible_deep_request_uses_economy_research_then_one_strong_synthesis(
        self,
    ) -> None:
        research_result = _successful_research_result()
        evidence_id = build_evidence_ledger(research_result.outcomes).evidence_ids[0]
        service = _StaticResearchService(research_result)
        orchestrator, foreground, normal_tools = _orchestrator(
            [(f"The primary evidence supports the comparison. [{evidence_id}]", [])],
            service=service,
        )
        metrics: dict[str, int | str] = {}

        answer, _ = await _run_orchestrator(
            orchestrator,
            request_text=(
                "Do a rigorous deep-dive comparing the latest Alpha and Beta reports "
                "with sources."
            ),
            metrics=metrics,
        )

        self.assertEqual(1, len(service.calls))
        self.assertEqual([], normal_tools.calls)
        self.assertEqual(1, len(foreground.calls))
        self.assertEqual("strong-model", foreground.calls[0]["model"])
        self.assertEqual("chat_reply", foreground.calls[0]["feature"])
        self.assertEqual([], foreground.calls[0]["tools"])
        self.assertEqual(
            {"economy-model"},
            {call.model for call in research_result.model_calls},
        )
        self.assertEqual("economy-model", metrics["deep_research_model"])
        self.assertEqual(0, metrics["exposed_tool_count"])
        self.assertEqual(3, metrics["tool_call_count"])
        self.assertEqual(3, metrics["agent_tool_call_count"])
        self.assertIn(evidence_id, answer)
        self.assertIn("Sources:", answer)
        self.assertIn("https://agency.gov/reports/alpha", answer)
        diagnostic_messages = str(metrics["_diagnostic_agent_messages_json"])
        self.assertIn("Composite deep research is complete", diagnostic_messages)
        self.assertIn("Evidence ledger", diagnostic_messages)

    async def test_quick_grounded_and_deep_stable_explanation_skip_composite(self) -> None:
        cases = (
            ("Tell me a joke", None),
            ("Can you help me think about this?", None),
            ("Explain recursion.", "deep"),
        )
        for request_text, depth_override in cases:
            with self.subTest(request_text=request_text, depth_override=depth_override):
                service = _StaticResearchService(_successful_research_result())
                orchestrator, foreground, _ = _orchestrator(
                    [("Direct answer.", [])],
                    service=service,
                )

                answer, _ = await _run_orchestrator(
                    orchestrator,
                    request_text=request_text,
                    depth_override=depth_override,
                )

                self.assertEqual("Direct answer.", answer)
                self.assertEqual([], service.calls)
                self.assertEqual(1, len(foreground.calls))

    async def test_total_composite_failure_retains_normal_tool_loop(self) -> None:
        service = _StaticResearchService(_failed_research_result())
        normal_outcome = ToolOutcome(
            call_id="normal-web",
            tool_name="web",
            arguments='{"query":"latest primary evidence"}',
            status=ToolStatus.OK,
            content="The normal web fallback found the official report.",
            provenance=("https://agency.gov/reports/fallback",),
        )
        evidence_id = build_evidence_ledger([normal_outcome]).evidence_ids[0]
        orchestrator, foreground, normal_tools = _orchestrator(
            [
                (
                    "",
                    [
                        LLMToolCall(
                            id="normal-web",
                            name="web",
                            arguments='{"query":"latest primary evidence"}',
                        )
                    ],
                ),
                (f"The fallback evidence supports the answer. [{evidence_id}]", []),
            ],
            service=service,
            normal_outcomes=[normal_outcome],
        )
        metrics: dict[str, int | str] = {}

        answer, _ = await _run_orchestrator(
            orchestrator,
            request_text="Do rigorous research on the latest result with sources.",
            metrics=metrics,
        )

        self.assertEqual(1, len(service.calls))
        self.assertEqual(1, len(normal_tools.calls))
        self.assertEqual("web", normal_tools.calls[0].name)
        self.assertEqual(2, len(foreground.calls))
        self.assertTrue(foreground.calls[0]["tools"])
        self.assertIn(evidence_id, answer)
        self.assertEqual("error", metrics["deep_research_status"])

    async def test_custom_tool_runner_disables_composite_path(self) -> None:
        service = _StaticResearchService(_successful_research_result())
        custom_tools = _RecordingToolRunner()
        orchestrator, foreground, _ = _orchestrator(
            [("Answer from the benchmark path.", [])],
            service=service,
        )

        answer, _ = await _run_orchestrator(
            orchestrator,
            request_text="Do rigorous research on the latest result with sources.",
            tool_runner=custom_tools,
        )

        self.assertEqual("Answer from the benchmark path.", answer)
        self.assertEqual([], service.calls)
        self.assertTrue(foreground.calls[0]["tools"])
        self.assertEqual([], custom_tools.calls)


class _EconomyLLM:
    def __init__(
        self,
        *,
        queries: tuple[str, ...],
        planning_text: str | None = None,
    ) -> None:
        self.queries = queries
        self.planning_text = planning_text
        self.calls: list[dict[str, object]] = []

    async def complete_chat(self, **kwargs: object) -> LLMResult:
        self.calls.append(dict(kwargs))
        feature = str(kwargs["feature"])
        if "plan" in feature:
            text = self.planning_text or json.dumps({"queries": list(self.queries)})
        else:
            text = json.dumps(
                {
                    "summary": "Condensed evidence from the observed source [S1].",
                    "source_ids": ["S1"],
                }
            )
        return LLMResult(
            text=text,
            usage=LLMUsage(
                feature=feature,
                model=str(kwargs["model"]),
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated_cost_usd=0.00001,
            ),
        )


class _ConcurrentTavilyClient:
    def __init__(
        self,
        *,
        failing_queries: set[str] | None = None,
        slow_queries: set[str] | None = None,
    ) -> None:
        self.failing_queries = failing_queries or set()
        self.slow_queries = slow_queries or set()
        self.searches: list[str] = []
        self.extracts: list[str] = []
        self.responses: dict[str, TavilySearchResponse] = {}
        self.active_searches = 0
        self.max_active_searches = 0

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        **_kwargs: object,
    ) -> TavilySearchResponse:
        self.searches.append(query)
        self.active_searches += 1
        self.max_active_searches = max(self.max_active_searches, self.active_searches)
        try:
            if query in self.slow_queries:
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.005)
            if query in self.failing_queries:
                raise RuntimeError(f"search failed for {query}")
            slug = str(len(self.responses) + 1)
            response = TavilySearchResponse(
                query=query,
                results=[
                    TavilySearchResult(
                        title=f"Official source for {query}",
                        url=f"https://official.example/{slug}",
                        content=f"Search evidence about {query}.",
                        score=0.95,
                    ),
                    TavilySearchResult(
                        title=f"Secondary source for {query}",
                        url=f"https://secondary.example/{slug}",
                        content=f"Corroborating evidence about {query}.",
                        score=0.80,
                    ),
                ][:max_results],
            )
            self.responses[query] = response
            return response
        finally:
            self.active_searches -= 1

    async def extract(
        self,
        url: str,
        *,
        query: str | None = None,
        **_kwargs: object,
    ) -> TavilyExtractResponse:
        self.extracts.append(url)
        await asyncio.sleep(0)
        return TavilyExtractResponse(
            url=url,
            query=query,
            results=[
                TavilyExtractResult(
                    url=url,
                    title="Official report",
                    raw_content=f"Extracted primary evidence for {query or url}.",
                )
            ],
        )


def _service(
    llm: _EconomyLLM,
    tavily: _ConcurrentTavilyClient,
    **config_overrides: object,
) -> CompositeDeepResearchService:
    config_values: dict[str, object] = {
        "economy_model": "economy-model",
        "model_timeout_seconds": 1.0,
        "search_timeout_seconds": 1.0,
        "extract_timeout_seconds": 1.0,
        "overall_timeout_seconds": 2.0,
    }
    config_values.update(config_overrides)
    return CompositeDeepResearchService(
        llm_client=llm,
        tavily_client=tavily,
        config=DeepResearchConfig(**config_values),
    )


class _StaticResearchService:
    def __init__(self, result: DeepResearchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, float | None]] = []

    async def research(
        self,
        question: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeepResearchResult:
        self.calls.append((question, timeout_seconds))
        return self.result


class _ForegroundLLM:
    def __init__(
        self,
        responses: list[tuple[str, list[LLMToolCall]]],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.provider_capabilities = SimpleNamespace(name="test")

    async def complete_chat_turn(self, **kwargs: object) -> LLMChatTurn:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra foreground model call")
        text, tool_calls = self.responses.pop(0)
        return LLMChatTurn(
            text=text,
            raw_text=text,
            usage=_usage(
                feature=str(kwargs["feature"]),
                model=str(kwargs["model"]),
            ),
            tool_calls=tool_calls,
            reasoning_content="",
            finish_reason="stop",
        )


class _RecordingToolRunner:
    def __init__(self, outcomes: list[ToolOutcome] | None = None) -> None:
        self.outcomes = outcomes or []
        self.calls: list[LLMToolCall] = []

    async def run(
        self,
        tool_calls: list[LLMToolCall],
        **_kwargs: object,
    ) -> list[ToolOutcome]:
        self.calls.extend(tool_calls)
        return self.outcomes


def _orchestrator(
    responses: list[tuple[str, list[LLMToolCall]]],
    *,
    service: _StaticResearchService,
    normal_outcomes: list[ToolOutcome] | None = None,
) -> tuple[ChatOrchestrator, _ForegroundLLM, _RecordingToolRunner]:
    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator.settings = SimpleNamespace(
        max_completion_tokens=700,
        openai_quick_model=None,
        openai_deep_model="strong-model",
        openai_reasoning_effort=None,
    )
    foreground = _ForegroundLLM(responses)
    normal_tools = _RecordingToolRunner(normal_outcomes)
    orchestrator.llm_client = foreground
    orchestrator.tool_runner = normal_tools
    orchestrator.deep_research_service = service
    orchestrator.agent_budget = AgentBudget()
    return orchestrator, foreground, normal_tools


async def _run_orchestrator(
    orchestrator: ChatOrchestrator,
    *,
    request_text: str,
    metrics: dict[str, int | str] | None = None,
    depth_override: str | None = None,
    tool_runner: _RecordingToolRunner | None = None,
) -> tuple[str, list[str]]:
    return await orchestrator.run_chat_with_tools(
        chat_model="default-model",
        messages=[{"role": "user", "content": request_text}],
        guild_id=None,
        channel_id=None,
        user_id=1,
        source_message_id=None,
        request_text=request_text,
        metrics=metrics,
        tool_runner=tool_runner,
        depth_override=depth_override,
    )


def _successful_research_result() -> DeepResearchResult:
    planning_usage = _usage(feature="deep_research_plan", model="economy-model")
    reduction_usage = _usage(feature="deep_research_reduce", model="economy-model")
    outcome = ToolOutcome(
        call_id="deep-research-1",
        tool_name="deep_research",
        arguments='{"query":"official Alpha report"}',
        status=ToolStatus.OK,
        content="The official Alpha report supports the result.",
        provenance=("https://agency.gov/reports/alpha",),
    )
    return DeepResearchResult(
        question="Compare Alpha and Beta",
        queries=("official Alpha report", "independent Beta report"),
        outcomes=(outcome,),
        model_calls=(
            DeepResearchModelCall(
                stage="planning",
                feature="deep_research_plan",
                model="economy-model",
                status="ok",
                latency_ms=1,
                usage=planning_usage,
            ),
            DeepResearchModelCall(
                stage="reduction",
                feature="deep_research_reduce",
                model="economy-model",
                status="ok",
                latency_ms=1,
                usage=reduction_usage,
                query="official Alpha report",
            ),
        ),
        search_calls=(
            DeepResearchSearchCall("official Alpha report", "ok", 2, 1),
            DeepResearchSearchCall("independent Beta report", "empty", 2, 0),
        ),
        extract_calls=(
            DeepResearchExtractCall(
                source_url="https://agency.gov/reports/alpha",
                status="ok",
                latency_ms=2,
                content_chars=50,
                used_snippet_fallback=False,
            ),
        ),
        used_fallback=False,
    )


def _failed_research_result() -> DeepResearchResult:
    planning_usage = _usage(feature="deep_research_plan", model="economy-model")
    queries = ("official result", "independent result")
    return DeepResearchResult(
        question="Research the result",
        queries=queries,
        outcomes=tuple(
            ToolOutcome(
                call_id=f"deep-research-{index}",
                tool_name="deep_research",
                arguments=json.dumps({"query": query}),
                status=ToolStatus.ERROR,
                content=f"Search failed for {query}.",
            )
            for index, query in enumerate(queries, start=1)
        ),
        model_calls=(
            DeepResearchModelCall(
                stage="planning",
                feature="deep_research_plan",
                model="economy-model",
                status="ok",
                latency_ms=1,
                usage=planning_usage,
            ),
        ),
        search_calls=tuple(
            DeepResearchSearchCall(query, "error", 2, 0, "RuntimeError")
            for query in queries
        ),
        extract_calls=(),
        used_fallback=True,
    )


def _usage(*, feature: str, model: str) -> LLMUsage:
    return LLMUsage(
        feature=feature,
        model=model,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost_usd=0.00001,
    )


if __name__ == "__main__":
    unittest.main()
