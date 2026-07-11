from __future__ import annotations

import json
import unittest

from nycti.chat.run_state import ToolStatus
from nycti.live_benchmark_fixture_tools import (
    execute_fixture_deep_research,
    execute_fixture_image_search,
    execute_fixture_memory_search,
    execute_fixture_url_extract,
    execute_fixture_web,
)
from nycti.live_benchmarks import (
    LiveBenchmarkExecution,
    LiveBenchmarkStatus,
    evaluate_live_benchmark,
    load_live_benchmark_manifest,
)


class LiveBenchmarkFixtureValidationTests(unittest.TestCase):
    def test_web_fixture_rejects_irrelevant_query(self) -> None:
        rejected = execute_fixture_web(
            '{"queries":["cat adoption"],"topic":null,"time_range":null}'
        )
        lumen = execute_fixture_web(
            '{"queries":["latest LumenOS release"],"topic":null,"time_range":null}'
        )
        pyra = execute_fixture_web(
            '{"queries":["Pyra 3.0 migration"],"topic":null,"time_range":null}'
        )

        self.assertEqual(ToolStatus.ERROR, rejected.status)
        self.assertEqual("invalid_inputs", rejected.metrics["web_search_status"])
        self.assertNotIn("LumenOS", rejected.content)
        self.assertEqual(ToolStatus.OK, lumen.status)
        self.assertIn("LumenOS 7.4", lumen.content)
        self.assertEqual(ToolStatus.OK, pyra.status)
        self.assertIn("lease sessions", pyra.content)

    def test_earnings_fixture_supports_search_extract_and_deep_research(self) -> None:
        web = execute_fixture_web(
            '{"queries":["latest NVIDIA and AMD earnings"],"topic":"finance","time_range":null}'
        )
        nvidia_extract = execute_fixture_url_extract(
            json.dumps(
                {
                    "url": (
                        "https://investor.nvidia.com/news/press-release-details/2026/"
                        "NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx"
                    ),
                    "query": "earnings",
                }
            )
        )
        amd_extract = execute_fixture_url_extract(
            json.dumps(
                {
                    "url": (
                        "https://ir.amd.com/news-events/press-releases/detail/1254/"
                        "amd-reports-first-quarter-2026-financial-results"
                    ),
                    "query": "earnings",
                }
            )
        )
        deep = execute_fixture_deep_research(
            _deep_arguments(
                question="Compare NVIDIA and AMD latest earnings",
                symbols=["NVDA", "AMD"],
            )
        )

        for result in (web, nvidia_extract, amd_extract, deep):
            self.assertEqual(ToolStatus.OK, result.status)
            self.assertTrue(result.provenance)
        self.assertIn("$81.615 billion", web.content)
        self.assertIn("$10.253 billion", web.content)
        self.assertIn("$91.0 billion", nvidia_extract.content)
        self.assertIn("$11.2 billion", amd_extract.content)
        self.assertEqual(2, deep.metrics["deep_research_source_count"])

    def test_image_fixture_requires_snowy_owl_query(self) -> None:
        rejected = execute_fixture_image_search('{"query":"barn owl"}')
        accepted = execute_fixture_image_search('{"query":"snowy owl in flight"}')

        self.assertEqual(ToolStatus.ERROR, rejected.status)
        self.assertEqual("invalid_inputs", rejected.metrics["image_search_status"])
        self.assertNotIn("snowy-owl.jpg", rejected.content)
        self.assertEqual(ToolStatus.OK, accepted.status)
        self.assertIn("snowy-owl.jpg", accepted.content)

    def test_generic_deep_fixture_requires_atlas_and_nova_question(self) -> None:
        rejected = execute_fixture_deep_research(
            _deep_arguments(question="Compare unrelated databases")
        )
        accepted = execute_fixture_deep_research(
            _deep_arguments(question="Compare AtlasDB and NovaDB")
        )

        self.assertEqual(ToolStatus.ERROR, rejected.status)
        self.assertEqual("invalid_inputs", rejected.metrics["deep_research_status"])
        self.assertNotIn("18,400", rejected.content)
        self.assertEqual(ToolStatus.OK, accepted.status)
        self.assertIn("18,400", accepted.content)
        self.assertIn("11.8", accepted.content)

    def test_composite_fixture_requires_relevant_specialized_inputs(self) -> None:
        accepted = execute_fixture_deep_research(
            _deep_arguments(
                question="Summarize the supplied inputs",
                urls=["https://bench.nycti.invalid/policy/"],
                symbols=["ACME"],
                youtube_urls=["https://youtu.be/benchNycti01/"],
                calculations=["result = 9173 * 62011;"],
            )
        )
        accepted_with_calculation_operand = execute_fixture_deep_research(
            _deep_arguments(
                question="Summarize the supplied inputs",
                urls=["https://bench.nycti.invalid/policy"],
                symbols=["ACME", "9173"],
                youtube_urls=["https://youtu.be/benchNycti01"],
                calculations=["result = 9173 * 62011"],
            )
        )
        rejected = execute_fixture_deep_research(
            _deep_arguments(
                question="Summarize the supplied inputs",
                urls=[
                    "https://bench.nycti.invalid/policy",
                    "https://wrong.invalid",
                ],
                symbols=["ACME", "WRONG"],
                youtube_urls=[
                    "https://youtu.be/benchNycti01",
                    "https://youtu.be/wrong",
                ],
                calculations=["9173*62011", "2+2"],
            )
        )

        self.assertEqual(ToolStatus.OK, accepted.status)
        self.assertEqual(ToolStatus.OK, accepted_with_calculation_operand.status)
        self.assertEqual(
            1,
            accepted_with_calculation_operand.metrics["deep_research_symbol_count"],
        )
        self.assertEqual(ToolStatus.ERROR, rejected.status)
        self.assertEqual(
            "invalid_inputs",
            rejected.metrics["deep_research_status"],
        )
        self.assertEqual(1, rejected.metrics["deep_research_invalid_input_count"])
        self.assertNotIn("568826903", rejected.content)

    def test_invalid_composite_inputs_score_as_failure_not_infrastructure_error(
        self,
    ) -> None:
        result = execute_fixture_deep_research(
            _deep_arguments(
                question="Summarize the supplied inputs",
                urls=["https://wrong.invalid"],
                symbols=["WRONG"],
                youtube_urls=["https://youtu.be/wrong"],
                calculations=["2+2"],
            )
        )
        case = load_live_benchmark_manifest().get_case("fixture-composite-mixed")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=result.content,
                called_tools=("deep_research",),
                successful_tools=(),
                metrics={**result.metrics, "agent_tool_call_count": 1},
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, evaluation.status)
        self.assertEqual("", evaluation.reason)
        self.assertIn("tool:succeeded:deep_research", "\n".join(evaluation.failed_checks))

    def test_shared_and_lore_memory_fixtures_enforce_owner_filter(self) -> None:
        cases = (
            ("shared project codename", "guild_shared", "Aster"),
            ("mascot lore", "lore", "Nyx"),
        )
        for query, scope, expected in cases:
            with self.subTest(scope=scope):
                rejected = execute_fixture_memory_search(
                    json.dumps(
                        {
                            "query": query,
                            "owner_user_ids": [999],
                            "visibility_scopes": [scope],
                        }
                    ),
                    requester_user_id=123,
                )
                accepted = execute_fixture_memory_search(
                    json.dumps(
                        {
                            "query": query,
                            "owner_user_ids": [123],
                            "visibility_scopes": [scope],
                        }
                    ),
                    requester_user_id=123,
                )

                self.assertEqual(ToolStatus.EMPTY, rejected.status)
                self.assertNotIn(expected, rejected.content)
                self.assertEqual(ToolStatus.OK, accepted.status)
                self.assertIn(expected, accepted.content)


def _deep_arguments(
    *,
    question: str,
    urls: list[str] | None = None,
    symbols: list[str] | None = None,
    youtube_urls: list[str] | None = None,
    calculations: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "question": question,
            "focus": None,
            "urls": urls,
            "symbols": symbols,
            "youtube_urls": youtube_urls,
            "calculations": calculations,
        }
    )


if __name__ == "__main__":
    unittest.main()
