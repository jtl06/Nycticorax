from __future__ import annotations

import asyncio
import json
import unittest

from nycti.chat.deep_research import (
    CompositeDeepResearchService,
    DeepResearchConfig,
    DeepResearchModelCall,
    DeepResearchResult,
    DeepResearchSearchCall,
    MAX_REDUCTION_INPUT_CHARS,
)
from nycti.chat.run_state import ToolOutcome, ToolStatus
from nycti.chat.tools.research import ResearchToolMixin
from nycti.chat.tools.parsing import parse_deep_research_arguments
from nycti.llm.types import LLMResult, LLMUsage
from nycti.tavily.models import (
    TavilyExtractResponse,
    TavilyExtractResult,
    TavilySearchResponse,
    TavilySearchResult,
)


def _usage(*, feature: str) -> LLMUsage:
    return LLMUsage(
        feature=feature,
        model="economy-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost_usd=0.00001,
    )


class DeepResearchToolParsingTests(unittest.TestCase):
    def test_parser_accepts_strict_nullable_and_composite_inputs(self) -> None:
        payload = parse_deep_research_arguments(
            '{"question":"Compare Alpha","focus":"primary sources",'
            '"urls":["https://alpha.example/filing"],"symbols":["alpha"],'
            '"youtube_urls":["https://youtu.be/example"],'
            '"calculations":["result = 2 + 2"]}'
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(("ALPHA",), payload.symbols)
        self.assertEqual(("https://alpha.example/filing",), payload.urls)
        self.assertEqual(("https://youtu.be/example",), payload.youtube_urls)
        self.assertEqual(("result = 2 + 2",), payload.calculations)

    def test_parser_rejects_invalid_composite_list_shape(self) -> None:
        self.assertIsNone(
            parse_deep_research_arguments(
                '{"question":"Research Alpha","focus":null,"urls":"not-a-list",'
                '"symbols":null,"youtube_urls":null,"calculations":null}'
            )
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


class CompositeResearchEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_reducer_message_contains_complete_parseable_evidence_json(self) -> None:
        class CapturingLLM:
            def __init__(self) -> None:
                self.reduction_messages: list[str] = []

            async def complete_chat(self, **kwargs: object) -> LLMResult:
                feature = str(kwargs["feature"])
                if feature == "deep_research_plan":
                    return LLMResult(
                        text=json.dumps(
                            {"queries": ["Q" * 240, "independent countercheck"]}
                        ),
                        usage=_usage(feature=feature),
                    )
                messages = kwargs["messages"]
                assert isinstance(messages, list)
                user_message = messages[-1]
                assert isinstance(user_message, dict)
                self.reduction_messages.append(str(user_message["content"]))
                return LLMResult(
                    text=(
                        '{"summary":"Supported by the supplied evidence [S1].",'
                        '"source_ids":["S1"]}'
                    ),
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
        marker = "Evidence JSON:\n"
        for message in llm.reduction_messages:
            self.assertLessEqual(len(message), MAX_REDUCTION_INPUT_CHARS)
            self.assertIn(marker, message)
            evidence = json.loads(message.split(marker, 1)[1])
            self.assertIsInstance(evidence, list)
            self.assertGreaterEqual(len(evidence), 1)

    async def test_title_only_search_hits_return_no_evidence(self) -> None:
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


class ModelCallableResearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_meta_tool_combines_web_url_finance_transcript_and_calculation(self) -> None:
        usage = _usage(feature="deep_research_plan")
        url = "https://agency.gov/report"
        research_result = DeepResearchResult(
            question="Compare Alpha",
            queries=("official Alpha", "independent Alpha"),
            outcomes=(
                ToolOutcome(
                    call_id="deep-1",
                    tool_name="deep_research",
                    arguments='{"query":"official Alpha"}',
                    status=ToolStatus.OK,
                    content=f"Official web evidence: {url}",
                    provenance=(url,),
                ),
            ),
            model_calls=(
                DeepResearchModelCall(
                    stage="planning",
                    feature="deep_research_plan",
                    model="economy-model",
                    status="ok",
                    latency_ms=1,
                    usage=usage,
                ),
            ),
            search_calls=(DeepResearchSearchCall("official Alpha", "ok", 1, 1),),
            extract_calls=(),
            used_fallback=False,
        )
        executor = _CompositeExecutor(_StaticResearchService(research_result))

        result = await executor._execute_deep_research_tool(
            question="Compare Alpha rigorously",
            focus="primary evidence",
            urls=("https://alpha.example/filing",),
            symbols=("ALPHA",),
            youtube_urls=("https://youtu.be/example",),
            calculations=("result = (120 - 100) / 100",),
            guild_id=7,
            channel_id=8,
            user_id=9,
        )

        self.assertEqual(ToolStatus.OK, result.status)
        self.assertIn("Official web evidence", result.content)
        self.assertIn("Tavily extract for", result.content)
        self.assertIn("Twelve Data market quote", result.content)
        self.assertIn("YouTube transcript summary", result.content)
        self.assertIn("Python result", result.content)
        self.assertEqual((usage,), result.usage_records)
        self.assertEqual(4, result.metrics["deep_research_specialized_call_count"])
        self.assertIn(url, result.provenance)

    async def test_meta_tool_preserves_specialized_evidence_before_large_web_output(self) -> None:
        web_url = "https://broad.example/report"
        exact_url = "https://alpha.example/filing"
        research_result = DeepResearchResult(
            question="Compare Alpha",
            queries=("broad Alpha research",),
            outcomes=(
                ToolOutcome(
                    call_id="deep-large",
                    tool_name="deep_research",
                    arguments='{"query":"broad Alpha research"}',
                    status=ToolStatus.OK,
                    content=f"Broad web evidence: {web_url}\n" + ("W" * 30_000),
                    provenance=(web_url,),
                ),
            ),
            model_calls=(),
            search_calls=(DeepResearchSearchCall("broad Alpha research", "ok", 1, 1),),
            extract_calls=(),
            used_fallback=False,
        )
        executor = _CompositeExecutor(_StaticResearchService(research_result))

        result = await executor._execute_deep_research_tool(
            question="Compare Alpha rigorously",
            focus=None,
            urls=(exact_url,),
            symbols=("ALPHA",),
            youtube_urls=("https://youtu.be/example",),
            calculations=("result = (120 - 100) / 100",),
            guild_id=7,
            channel_id=8,
            user_id=9,
        )

        self.assertLessEqual(len(result.content), 16_000)
        self.assertIn(f"Tavily extract for: {exact_url}", result.content)
        self.assertIn("Twelve Data market quote", result.content)
        self.assertIn("YouTube transcript summary", result.content)
        self.assertIn("Python result", result.content)
        self.assertEqual(exact_url, result.provenance[0])
        self.assertIn(web_url, result.provenance)

    async def test_cancelling_meta_tool_cancels_composite_service(self) -> None:
        service = _BlockingResearchService()
        executor = _CompositeExecutor(service)
        task = asyncio.create_task(
            executor._execute_deep_research_tool(
                question="Research this",
                focus=None,
                urls=(),
                symbols=(),
                youtube_urls=(),
                calculations=(),
                guild_id=None,
                channel_id=None,
                user_id=1,
            )
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(service.cancelled)


class _StaticResearchService:
    def __init__(self, result: DeepResearchResult) -> None:
        self.result = result

    async def research(
        self,
        question: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeepResearchResult:
        return self.result


class _BlockingResearchService:
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


class _CompositeExecutor(ResearchToolMixin):
    def __init__(self, service: object) -> None:
        self.deep_research_service = service  # type: ignore[assignment]

    async def _execute_extract_url_tool(self, *, url: str, query: str | None) -> str:
        return f"Tavily extract for: {url}\nPrimary filing evidence."

    async def _execute_stock_quote_tool(self, *, symbols: list[str]) -> str:
        return f"Twelve Data market quote for: Alpha ({symbols[0]})\nLast price: 10.0000"

    async def _execute_youtube_transcript_tool(self, **kwargs: object) -> tuple[str, int]:
        return f"YouTube transcript summary for: {kwargs['url']}\nSpeaker evidence.", 12

    async def _execute_python_tool(self, *, code: str) -> str:
        return "Python result (1 ms):\nresult = 0.2"


if __name__ == "__main__":
    unittest.main()
