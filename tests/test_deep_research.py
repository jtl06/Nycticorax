from __future__ import annotations

import asyncio
import hashlib
import time
import unittest

from nycti.chat.deep_research import (
    CompositeDeepResearchService,
    DeepResearchConfig,
)
from nycti.chat.run_state import ToolStatus
from nycti.llm.types import LLMResult, LLMUsage
from nycti.tavily.models import (
    TavilyExtractResponse,
    TavilyExtractResult,
    TavilySearchResponse,
    TavilySearchResult,
)


def _llm_result(text: str, *, feature: str, tokens: int = 20) -> LLMResult:
    return LLMResult(
        text=text,
        usage=LLMUsage(
            feature=feature,
            model="economy-model",
            prompt_tokens=tokens - 5,
            completion_tokens=5,
            total_tokens=tokens,
            estimated_cost_usd=0.00001,
        ),
    )


class _HappyLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.active_reductions = 0
        self.max_active_reductions = 0

    async def complete_chat(self, **kwargs: object) -> LLMResult:
        self.calls.append(dict(kwargs))
        feature = str(kwargs["feature"])
        if feature == "deep_research_plan":
            return _llm_result(
                '{"queries":["official alpha evidence","independent beta evidence"]}',
                feature=feature,
            )
        self.active_reductions += 1
        self.max_active_reductions = max(
            self.max_active_reductions,
            self.active_reductions,
        )
        await asyncio.sleep(0.01)
        self.active_reductions -= 1
        return _llm_result(
            '{"summary":"The extracted evidence supports the finding [S1].",'
            '"source_ids":["S1"]}',
            feature=feature,
        )


class _HappyTavily:
    def __init__(self) -> None:
        self.active_searches = 0
        self.max_active_searches = 0
        self.extracted_urls: list[str] = []

    async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
        self.active_searches += 1
        self.max_active_searches = max(self.max_active_searches, self.active_searches)
        await asyncio.sleep(0.01)
        self.active_searches -= 1
        slug = "alpha" if "alpha" in query else "beta"
        return TavilySearchResponse(
            query=query,
            results=[
                TavilySearchResult(
                    title=f"Official {slug} report",
                    url=f"https://agency.gov/reports/{slug}",
                    content=f"Search snippet for {slug}.",
                    score=0.8,
                    published_date="2026-07-01",
                ),
                TavilySearchResult(
                    title=f"Commentary about {slug}",
                    url=f"https://medium.com/{slug}",
                    content=f"Secondary discussion for {slug}.",
                    score=0.99,
                ),
            ],
        )

    async def extract(self, url: str, **kwargs: object) -> TavilyExtractResponse:
        self.extracted_urls.append(url)
        await asyncio.sleep(0)
        return TavilyExtractResponse(
            url=url,
            query=str(kwargs.get("query") or ""),
            results=[
                TavilyExtractResult(
                    url=url,
                    title="Official report",
                    raw_content=f"Extracted authoritative evidence from {url}.",
                )
            ],
        )


class CompositeDeepResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_parallel_search_extract_reduce_and_exposes_usage(self) -> None:
        llm = _HappyLLM()
        tavily = _HappyTavily()
        service = CompositeDeepResearchService(
            llm_client=llm,  # type: ignore[arg-type]
            tavily_client=tavily,  # type: ignore[arg-type]
            config=DeepResearchConfig(
                economy_model="economy-model",
                max_extracts=2,
            ),
        )

        result = await service.research("Compare alpha and beta rigorously")

        self.assertEqual(
            result.queries,
            ("official alpha evidence", "independent beta evidence"),
        )
        self.assertTrue(result.has_evidence)
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.used_fallback)
        self.assertEqual(len(result.outcomes), 2)
        self.assertTrue(all(outcome.status == ToolStatus.OK for outcome in result.outcomes))
        self.assertTrue(
            all("Extracted authoritative evidence" in outcome.content for outcome in result.outcomes)
        )
        self.assertEqual(len(result.usages), 3)
        self.assertTrue(all(usage.model == "economy-model" for usage in result.usages))
        self.assertEqual(tavily.max_active_searches, 2)
        self.assertEqual(llm.max_active_reductions, 2)
        self.assertEqual(len(result.extract_calls), 2)
        self.assertTrue(all(call.status == "ok" for call in result.extract_calls))
        self.assertTrue(all("tools" not in call for call in llm.calls))
        self.assertTrue(
            all(call["model"] == "economy-model" for call in llm.calls)
        )

    async def test_falls_back_to_snippets_when_each_optional_stage_fails(self) -> None:
        class FailingLLM:
            async def complete_chat(self, **kwargs: object) -> LLMResult:
                feature = str(kwargs["feature"])
                if feature == "deep_research_plan":
                    return _llm_result("not json", feature=feature)
                raise RuntimeError("reduction unavailable")

        class ExtractFailingTavily:
            async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
                suffix = hashlib.sha256(query.encode()).hexdigest()[:10]
                return TavilySearchResponse(
                    query=query,
                    results=[
                        TavilySearchResult(
                            title="Useful source",
                            url=f"https://example.com/{suffix}",
                            content="Deterministic snippet evidence remains usable.",
                            score=0.7,
                        )
                    ],
                )

            async def extract(self, url: str, **kwargs: object) -> TavilyExtractResponse:
                raise RuntimeError("extract unavailable")

        service = CompositeDeepResearchService(
            llm_client=FailingLLM(),  # type: ignore[arg-type]
            tavily_client=ExtractFailingTavily(),  # type: ignore[arg-type]
            config=DeepResearchConfig(economy_model="economy-model"),
        )

        result = await service.research("How reliable is this method?")

        self.assertEqual(len(result.queries), 2)
        self.assertTrue(result.has_evidence)
        self.assertEqual(result.status, "partial")
        self.assertTrue(result.used_fallback)
        self.assertTrue(
            all("Deterministic snippet evidence" in outcome.content for outcome in result.outcomes)
        )
        self.assertTrue(all(call.status == "error" for call in result.extract_calls))
        self.assertEqual(result.metrics["deep_research_extract_failure_count"], 2)
        self.assertEqual(result.metrics["deep_research_reduction_fallback_count"], 2)
        self.assertEqual(len(result.usages), 1)

    async def test_primary_source_ranking_controls_bounded_extraction(self) -> None:
        llm = _HappyLLM()
        tavily = _HappyTavily()
        service = CompositeDeepResearchService(
            llm_client=llm,  # type: ignore[arg-type]
            tavily_client=tavily,  # type: ignore[arg-type]
            config=DeepResearchConfig(
                economy_model="economy-model",
                max_extracts=1,
            ),
        )

        result = await service.research("Compare alpha and beta")

        self.assertTrue(result.has_evidence)
        self.assertEqual(len(tavily.extracted_urls), 1)
        self.assertIn("agency.gov", tavily.extracted_urls[0])

    async def test_first_party_pages_rank_above_community_subdomains(self) -> None:
        class CommunityFirstTavily(_HappyTavily):
            async def search(self, query: str, **kwargs: object) -> TavilySearchResponse:
                return TavilySearchResponse(
                    query=query,
                    results=[
                        TavilySearchResult(
                            title="Community discussion",
                            url="https://community.vendor.example/latest-model-thread",
                            content="A forum discussion of the release.",
                            score=0.99,
                        ),
                        TavilySearchResult(
                            title="Product announcement",
                            url="https://vendor.example/latest-model",
                            content="The publisher's product announcement.",
                            score=0.80,
                        ),
                    ],
                )

        llm = _HappyLLM()
        tavily = CommunityFirstTavily()
        service = CompositeDeepResearchService(
            llm_client=llm,  # type: ignore[arg-type]
            tavily_client=tavily,  # type: ignore[arg-type]
            config=DeepResearchConfig(economy_model="economy-model", max_extracts=1),
        )

        await service.research("What is Vendor's latest model?")

        self.assertEqual(["https://vendor.example/latest-model"], tavily.extracted_urls)
        planning_messages = llm.calls[0]["messages"]
        self.assertIn("site:<publisher-domain>", str(planning_messages))

    async def test_overall_deadline_stops_later_stages(self) -> None:
        class SlowLLM:
            async def complete_chat(self, **kwargs: object) -> LLMResult:
                await asyncio.sleep(1)
                return _llm_result('{"queries":["one","two"]}', feature=str(kwargs["feature"]))

        tavily = _HappyTavily()
        service = CompositeDeepResearchService(
            llm_client=SlowLLM(),  # type: ignore[arg-type]
            tavily_client=tavily,  # type: ignore[arg-type]
            config=DeepResearchConfig(
                economy_model="economy-model",
                overall_timeout_seconds=0.02,
            ),
        )
        started_at = time.perf_counter()

        result = await service.research("A bounded question")

        self.assertLess(time.perf_counter() - started_at, 0.2)
        self.assertFalse(result.has_evidence)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.model_calls[0].status, "timeout")
        self.assertTrue(all(call.status == "skipped" for call in result.search_calls))
        self.assertEqual(tavily.extracted_urls, [])

    async def test_rejects_blank_question_and_invalid_timeout(self) -> None:
        service = CompositeDeepResearchService(
            llm_client=_HappyLLM(),  # type: ignore[arg-type]
            tavily_client=_HappyTavily(),  # type: ignore[arg-type]
            config=DeepResearchConfig(economy_model="economy-model"),
        )

        with self.assertRaises(ValueError):
            await service.research("  ")
        with self.assertRaises(ValueError):
            await service.research("valid", timeout_seconds=0)


class DeepResearchConfigTests(unittest.TestCase):
    def test_validates_hard_bounds(self) -> None:
        with self.assertRaises(ValueError):
            DeepResearchConfig(economy_model="")
        with self.assertRaises(ValueError):
            DeepResearchConfig(economy_model="economy", min_queries=1)
        with self.assertRaises(ValueError):
            DeepResearchConfig(economy_model="economy", max_queries=5)
        with self.assertRaises(ValueError):
            DeepResearchConfig(economy_model="economy", max_extracts=9)


if __name__ == "__main__":
    unittest.main()
