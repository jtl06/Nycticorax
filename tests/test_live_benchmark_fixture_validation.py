from __future__ import annotations

import json
import asyncio
import re
import unittest

from nycti.chat.run_state import AgentPermissions, ToolStatus
from nycti.live_benchmark_fixture_tools import (
    execute_fixture_deep_research,
    execute_fixture_image_search,
    execute_fixture_memory_search,
    execute_fixture_price_history,
    execute_fixture_quote,
    execute_fixture_url_extract,
    execute_fixture_web,
)
from nycti.live_benchmarks import (
    LiveBenchmarkExecution,
    LiveBenchmarkFixtureExecutor,
    LiveBenchmarkStatus,
    evaluate_live_benchmark,
    load_live_benchmark_manifest,
)


class LiveBenchmarkFixtureValidationTests(unittest.TestCase):
    def test_news_time_accepts_equivalent_formatting_not_wrong_time(self) -> None:
        pattern = load_live_benchmark_manifest().get_case("fixture-fresh-news").checks.answer_regex[1]
        for answer in ("09:30 local", "9:30 a.m."):
            self.assertIsNotNone(re.search(pattern, answer))
        for answer in ("19:30", "09:31", "9:00"):
            self.assertIsNone(re.search(pattern, answer))

    def test_earnings_dates_allow_shared_year_but_reject_explicit_wrong_year(self) -> None:
        patterns = load_live_benchmark_manifest().get_case("fixture-earnings-comparison").checks.answer_regex
        for pattern, day in ((patterns[3], "20"), (patterns[8], "5")):
            for answer in (f"May {day}, 2026", f"As of July 2026: May {day}."):
                self.assertIsNotNone(re.search(pattern, answer))
            for answer in (f"May {day}, 2025", f"May {day}1, 2026"):
                self.assertIsNone(re.search(pattern, answer))
        self.assertIn(r"\b2026\b", patterns)

    def test_overnight_quote_and_production_report_share_typed_values(self) -> None:
        async def run():
            for case_id in ("fixture-market-overnight-session", "fixture-market-casual-ah-boundary"):
                case = load_live_benchmark_manifest().get_case(case_id)
                executor = LiveBenchmarkFixtureExecutor(case.tool_fixtures)
                common = dict(guild_id=None, channel_id=None, user_id=1, source_message_id=None,
                              permissions=AgentPermissions(), run_id="fixture", step_index=1)
                quote = await executor.execute(tool_name="quote", arguments='{"symbols":["ACME"]}', **common)
                report = await executor.execute(tool_name="market_report",
                    arguments='{"symbols":["ACME"],"session":"overnight","finish":true}', **common)
                self.assertEqual(ToolStatus.OK, quote.status)
                self.assertEqual(ToolStatus.OK, report.status)
                self.assertIn("$102.00", report.direct_reply)
                self.assertIn("+2.00%", report.content)
                self.assertIn("$100.00", report.content)
                self.assertIn("overnight", report.content)
        asyncio.run(run())

    def test_typed_market_snapshot_rejects_unknown_or_nonfinite_fields(self) -> None:
        from nycti.live_benchmark_market import parse_market_snapshot
        for raw in ({"symbol": "ACME", "extended_price": float("nan")},
                    {"symbol": "ACME", "invented": 1}, {"symbol": 1}):
            with self.assertRaises(ValueError):
                parse_market_snapshot(raw)

    def test_overnight_percentage_accepts_equivalent_discord_formatting(self) -> None:
        case = load_live_benchmark_manifest().get_case("fixture-market-overnight-session")
        pattern = case.checks.answer_regex[1]
        for answer in ("up $2.00 / 2.0%", "up **$2.00 / 2.0%**", "+2.00%", "rose 2%"):
            with self.subTest(answer=answer):
                self.assertIsNotNone(re.search(pattern, answer, re.IGNORECASE))
        for answer in ("up 5.26%", "down 2.0%", "up 12.0%"):
            with self.subTest(answer=answer):
                self.assertIsNone(re.search(pattern, answer, re.IGNORECASE))

    def test_explicit_close_accepts_history_instead_of_forcing_quote(self) -> None:
        case = load_live_benchmark_manifest().get_case("fixture-market-explicit-close")
        self.assertFalse(case.tool_fixtures)
        self.assertFalse(case.checks.required_tools)
        self.assertIn("price_hist", case.checks.required_any_tools)
        result = execute_fixture_price_history('{"symbol":"ACME","interval":"1day","outputsize":5}')
        self.assertEqual(ToolStatus.OK, result.status)
        self.assertIn("2026-07-09 close $136.00", result.content)

    def test_market_fixture_supports_broad_and_watchlist_quote_batches(self) -> None:
        result = execute_fixture_quote(
            '{"symbols":["SPY","QQQ","SOXX","NVDA","AMD","MU","MemberB"]}'
        )

        self.assertEqual(ToolStatus.OK, result.status)
        self.assertEqual("mixed", result.metrics["stock_quote_status"])
        self.assertEqual(6, result.metrics["stock_quote_success_symbol_count"])
        self.assertIn("NVDA", result.content)
        self.assertIn("Unresolved symbols: MEMBERB", result.content)

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

    def test_database_evidence_is_available_without_the_meta_tool(self) -> None:
        search = execute_fixture_web('{"queries":["AtlasDB vs NovaDB"]}')
        self.assertEqual(ToolStatus.OK, search.status)
        self.assertEqual(2, len(search.provenance))
        for url in search.provenance:
            extracted = execute_fixture_url_extract(json.dumps({"url": url}))
            self.assertEqual(ToolStatus.OK, extracted.status)
            self.assertIn(extracted.content.split("\n", 1)[1], search.content)

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
        self.assertTrue(any(check.startswith("answer:matches") for check in evaluation.failed_checks))

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
            **({"urls": urls} if urls is not None else {}),
            **({"symbols": symbols} if symbols is not None else {}),
            **({"youtube_urls": youtube_urls} if youtube_urls is not None else {}),
            **({"calculations": calculations} if calculations is not None else {}),
        }
    )


if __name__ == "__main__":
    unittest.main()
