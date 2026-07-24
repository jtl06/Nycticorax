from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from nycti.chat.run_state import AgentPermissions, ToolStatus
from nycti.live_benchmarks import (
    DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH,
    LIVE_BENCHMARK_FIXTURE_TOOL_NAMES,
    MAX_LIVE_BENCHMARK_PROMPT_CHARS,
    LiveBenchmarkExecution,
    LiveBenchmarkFixtureExecutor,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    build_live_benchmark_fixture_tool_runner,
    evaluate_live_benchmark,
    extract_called_tools,
    load_live_benchmark_manifest,
    parse_live_benchmark_manifest,
    run_live_benchmark_suite,
)


class LiveBenchmarkManifestTests(unittest.TestCase):
    def test_default_manifest_has_short_fixture_and_canary_prompts(self) -> None:
        manifest = load_live_benchmark_manifest()

        self.assertEqual(7, manifest.version)
        self.assertTrue(
            {
                "fixture-earnings-comparison",
                "fixture-channel-decision",
                "fixture-memory-prefetch",
                "canary-spacex-price",
                "canary-semis-sector",
            }.issubset({case.case_id for case in manifest.cases})
        )
        self.assertEqual(
            {LiveBenchmarkMode.FIXTURES, LiveBenchmarkMode.CANARIES},
            {case.mode for case in manifest.cases},
        )
        self.assertTrue(
            all(
                0 < len(case.prompt) <= MAX_LIVE_BENCHMARK_PROMPT_CHARS
                for case in manifest.cases
            )
        )

        memory_case = manifest.get_case("fixture-memory-prefetch")
        self.assertIn("Uses Helix", memory_case.context.memories)
        self.assertIn("owner_user_id=9000000002", memory_case.context.memories)
        self.assertIn("concise replies", memory_case.context.personal_profile)

    def test_manifest_rejects_synthetic_context_on_live_canary(self) -> None:
        raw = _manifest_raw()
        raw["cases"][0]["mode"] = "canaries"
        raw["cases"][0]["context"] = {"memories": "- synthetic memory"}

        with self.assertRaisesRegex(ValueError, "allowed only for fixture"):
            parse_live_benchmark_manifest(raw)

    def test_manifest_bounds_synthetic_context(self) -> None:
        raw = _manifest_raw()
        raw["cases"][0]["context"] = {"memories": "x" * 2_001}

        with self.assertRaisesRegex(ValueError, "exceeds 2000"):
            parse_live_benchmark_manifest(raw)

    def test_default_manifest_scores_deep_research_only_for_justified_cases(self) -> None:
        manifest = load_live_benchmark_manifest()
        deep_allowed = {
            "fixture-earnings-comparison",
            "fixture-deep-comparison",
            "fixture-composite-mixed",
            "canary-deep-openai",
        }

        for case in manifest.cases:
            with self.subTest(case=case.case_id):
                if case.case_id in deep_allowed:
                    self.assertNotIn("deep_research", case.checks.forbidden_tools)
                else:
                    self.assertIn("deep_research", case.checks.forbidden_tools)

        self.assertEqual(
            ("web", "url_extract"),
            manifest.get_case("canary-openai-latest").checks.required_any_tools,
        )
        self.assertEqual(
            ("web",),
            manifest.get_case("canary-openai-news").checks.required_any_tools,
        )

    def test_fixture_corpus_covers_every_deterministic_fixture_tool(self) -> None:
        fixture_cases = [
            case
            for case in load_live_benchmark_manifest().cases
            if case.mode == LiveBenchmarkMode.FIXTURES
        ]
        required = {
            tool
            for case in fixture_cases
            for tool in (
                *case.checks.required_tools,
                *case.checks.required_attempted_tools,
                *case.checks.required_any_tools,
            )
        }

        self.assertEqual(LIVE_BENCHMARK_FIXTURE_TOOL_NAMES, required)

    def test_default_path_is_explicitly_root_benchmarks_directory(self) -> None:
        self.assertEqual("live_cases.json", DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH.name)
        self.assertEqual("benchmarks", DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH.parent.name)
        self.assertTrue(DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH.is_file())

    def test_loader_rejects_prompt_over_120_characters(self) -> None:
        raw = _manifest_raw(prompt="x" * (MAX_LIVE_BENCHMARK_PROMPT_CHARS + 1))

        with self.assertRaisesRegex(ValueError, "maximum is 120"):
            parse_live_benchmark_manifest(raw)

    def test_loader_rejects_duplicate_ids_unknown_fields_and_bad_regex(self) -> None:
        duplicate = _manifest_raw()
        duplicate["cases"].append(dict(duplicate["cases"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_live_benchmark_manifest(duplicate)

        unknown = _manifest_raw()
        unknown["cases"][0]["mystery"] = True
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_live_benchmark_manifest(unknown)

        invalid_regex = _manifest_raw()
        invalid_regex["cases"][0]["checks"] = {"answer_regex": ["("]}
        with self.assertRaisesRegex(ValueError, "invalid regex"):
            parse_live_benchmark_manifest(invalid_regex)

    def test_loader_validates_thresholded_regex_groups(self) -> None:
        valid = _manifest_raw()
        valid["cases"][0]["checks"] = {
            "answer_regex_groups": [
                {
                    "patterns": [r"\bNVDA\b", r"\bAMD\b"],
                    "minimum": 1,
                    "case_sensitive": True,
                }
            ]
        }
        parsed = parse_live_benchmark_manifest(valid)

        group = parsed.cases[0].checks.answer_regex_groups[0]
        self.assertEqual(1, group.minimum)
        self.assertTrue(group.case_sensitive)

        for invalid_group, error in (
            ({"patterns": [], "minimum": 1}, "patterns must not be empty"),
            (
                {"patterns": [r"\bNVDA\b", r"\bNVDA\b"], "minimum": 1},
                "patterns must be unique",
            ),
            ({"patterns": [r"\bNVDA\b"], "minimum": 2}, "minimum must be between"),
            ({"patterns": ["("], "minimum": 1}, "invalid regex"),
            (
                {"patterns": [r"\bNVDA\b"], "minimum": 1, "case_sensitive": "yes"},
                "case_sensitive must be a boolean",
            ),
        ):
            with self.subTest(error=error):
                raw = _manifest_raw()
                raw["cases"][0]["checks"] = {
                    "answer_regex_groups": [invalid_group]
                }
                with self.assertRaisesRegex(ValueError, error):
                    parse_live_benchmark_manifest(raw)

    def test_loader_validates_forbidden_tools_and_required_overlap(self) -> None:
        valid = _manifest_raw()
        valid["cases"][0]["checks"] = {"forbidden_tools": ["deep_research"]}

        parsed = parse_live_benchmark_manifest(valid)

        self.assertEqual(
            ("deep_research",),
            parsed.cases[0].checks.forbidden_tools,
        )

        for required_key in (
            "required_tools",
            "required_attempted_tools",
            "required_any_tools",
        ):
            with self.subTest(required_key=required_key):
                overlap = _manifest_raw()
                overlap["cases"][0]["checks"] = {
                    required_key: ["deep_research"],
                    "forbidden_tools": ["deep_research"],
                }
                with self.assertRaisesRegex(ValueError, "both requires and forbids"):
                    parse_live_benchmark_manifest(overlap)

        unknown = _manifest_raw()
        unknown["cases"][0]["checks"] = {"forbidden_tools": ["not_a_tool"]}
        with self.assertRaisesRegex(ValueError, "unknown tools"):
            parse_live_benchmark_manifest(unknown)

    def test_missing_manifest_has_deployment_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(FileNotFoundError, "Deploy.*benchmarks/live_cases.json"):
                load_live_benchmark_manifest(missing)


class LiveBenchmarkScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_live_benchmark_manifest()

    def test_structural_scoring_passes_exact_calculation(self) -> None:
        case = self.manifest.get_case("fixture-calculation")
        execution = LiveBenchmarkExecution(
            answer="568826903",
            metrics={
                **_fixture_slo_metrics(),
                "routing_called_tools": "calc",
                "routing_successful_tools": "calc",
                "agent_tool_call_count": 1,
            },
        )

        evaluation = evaluate_live_benchmark(case, execution)

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)
        self.assertEqual(evaluation.max_score, evaluation.score)
        self.assertEqual((), evaluation.failed_checks)

    def test_forbidden_tool_fails_even_when_its_call_did_not_succeed(self) -> None:
        case = self.manifest.get_case("fixture-calculation")
        execution = LiveBenchmarkExecution(
            answer="568826903",
            called_tools=("deep_research", "calc"),
            successful_tools=("calc",),
            metrics={
                **_fixture_slo_metrics(),
                "agent_tool_call_count": 2,
            },
        )

        evaluation = evaluate_live_benchmark(case, execution)

        self.assertEqual(LiveBenchmarkStatus.FAIL, evaluation.status)
        self.assertIn(
            "tool:not_called:deep_research",
            "\n".join(evaluation.failed_checks),
        )

    def test_calculation_scoring_accepts_grouping_but_rejects_wrong_or_signed_values(self) -> None:
        case = self.manifest.get_case("fixture-calculation")
        metrics = {
            **_fixture_slo_metrics(),
            "routing_called_tools": "calc",
            "routing_successful_tools": "calc",
            "agent_tool_call_count": 1,
        }

        grouped = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="568,826,903.", metrics=metrics),
        )
        negative = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="-568,826,903", metrics=metrics),
        )
        wrong = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="568,826,904", metrics=metrics),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, grouped.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, negative.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, wrong.status)

    def test_composite_calculation_accepts_sentence_punctuation(self) -> None:
        case = self.manifest.get_case("fixture-composite-mixed")
        answer = (
            "ACME is $137.25. 9173 × 62011 = 568,826,903. "
            "The policy allows 37 requests and the rollout uses shadow traffic."
        )
        metrics = {
            **_fixture_slo_metrics(),
            "routing_called_tools": "deep_research",
            "routing_successful_tools": "deep_research",
            "routing_grounding_quality_score": 100,
            "agent_tool_call_count": 1,
            "deep_research_specialized_call_count": 4,
            "deep_research_url_count": 1,
            "deep_research_symbol_count": 1,
            "deep_research_transcript_count": 1,
            "deep_research_calculation_count": 1,
        }

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer=answer, metrics=metrics),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)

    def test_missing_url_accepts_candid_limitations_without_accepting_fabrication(self) -> None:
        case = self.manifest.get_case("fixture-honest-missing-url")
        metrics = {
            **_fixture_slo_metrics(),
            "routing_called_tools": "url_extract",
            "agent_tool_call_count": 1,
        }

        candid_answers = (
            "I couldn’t read it: that URL has no available page/fixture.",
            "That URL doesn't resolve to anything.",
            "The supplied address isn't a real domain.",
            "There's no page to read at that URL.",
            "That page doesn't exist.",
            "This address doesn't resolve.",
        )
        for answer in candid_answers:
            with self.subTest(answer=answer):
                evaluation = evaluate_live_benchmark(
                    case,
                    LiveBenchmarkExecution(answer=answer, metrics=metrics),
                )
                self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)

        fabricated = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=(
                    "The URL doesn't resolve normally, but according to the page, "
                    "the policy limit is 37 requests."
                ),
                metrics=metrics,
            ),
        )
        self.assertEqual(LiveBenchmarkStatus.FAIL, fabricated.status)
        self.assertIn("answer:forbidden:1", "\n".join(fabricated.failed_checks))

        contains_claim = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="That address doesn't resolve, but the page contains a 37-request limit.",
                metrics=metrics,
            ),
        )
        self.assertEqual(LiveBenchmarkStatus.FAIL, contains_claim.status)

    def test_structural_scoring_reports_missing_answer_fact_and_tool(self) -> None:
        case = self.manifest.get_case("fixture-calculation")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="About 500 million.", metrics={}),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, evaluation.status)
        failed = "\n".join(evaluation.failed_checks)
        self.assertIn("answer:matches:1", failed)
        self.assertIn("tool:succeeded:calc", failed)

    def test_attempted_but_failed_required_tool_cannot_pass(self) -> None:
        case = self.manifest.get_case("fixture-calculation")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="568826903",
                called_tools=("calc",),
                successful_tools=(),
                metrics={"agent_tool_call_count": 1},
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, evaluation.status)
        self.assertIn("tool:succeeded:calc", "\n".join(evaluation.failed_checks))

    def test_grounding_accepts_quality_metric_and_rejects_unscored_answer(self) -> None:
        case = self.manifest.get_case("fixture-fresh-release")
        base = {
            **_fixture_slo_metrics(),
            "routing_called_tools": "web",
            "routing_successful_tools": "web",
            "agent_tool_call_count": 1,
        }

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="LumenOS 7.4 is latest.",
                metrics={**base, "routing_grounding_quality_score": 100},
            ),
        )
        failed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="LumenOS 7.4 is latest.", metrics=base),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, failed.status)
        self.assertIn("grounding:valid", "\n".join(failed.failed_checks))

    def test_provider_failure_metrics_are_error_not_quality_failure(self) -> None:
        case = self.manifest.get_case("fixture-quick-recursion")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="I couldn't generate a clean reply.",
                metrics={
                    "agent_stop_reason": "provider_error",
                    "agent_provider_error_count": 1,
                    "agent_model_turn_count": 0,
                    "agent_final_status": "fallback",
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.ERROR, evaluation.status)
        self.assertIn("provider", evaluation.reason)
        self.assertEqual(0, evaluation.max_score)

    def test_recovered_provider_attempt_can_still_be_scored(self) -> None:
        case = self.manifest.get_case("fixture-quick-recursion")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Recursion is a process that calls itself with a smaller input.",
                metrics={
                    "agent_stop_reason": "final_text",
                    "agent_provider_error_count": 1,
                    "agent_model_turn_count": 1,
                    "agent_final_status": "recovered",
                    "agent_tool_call_count": 0,
                    "exposed_tool_count": 12,
                    "reply_generation_ms": 5000,
                    "agent_total_tokens": 3000,
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)

    def test_quick_case_fails_declared_speed_budget(self) -> None:
        case = self.manifest.get_case("fixture-quick-recursion")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Recursion calls itself until it reaches a base case.",
                metrics={
                    "agent_stop_reason": "final_text",
                    "agent_model_turn_count": 1,
                    "agent_tool_call_count": 0,
                    "exposed_tool_count": 12,
                    "reply_generation_ms": 12000,
                    "agent_total_tokens": 3000,
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, evaluation.status)
        self.assertIn("metric:max:reply_generation_ms", "\n".join(evaluation.failed_checks))

    def test_tool_provider_status_is_error_not_answer_quality_failure(self) -> None:
        case = self.manifest.get_case("fixture-market-quote")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="The quote service failed.",
                called_tools=("quote",),
                metrics={"stock_quote_status": "http_error"},
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.ERROR, evaluation.status)
        self.assertIn("tool provider", evaluation.reason)

    def test_tool_timeout_is_error_not_answer_quality_failure(self) -> None:
        case = self.manifest.get_case("fixture-fresh-release")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Search timed out.",
                called_tools=("web",),
                metrics={"tool_timeout_count": 1},
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.ERROR, evaluation.status)
        self.assertIn("timed out", evaluation.reason)

    def test_recovered_tool_timeout_can_still_be_scored(self) -> None:
        case = self.manifest.get_case("fixture-fresh-release")

        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="LumenOS 7.4 is the latest release.",
                metrics={
                    **_fixture_slo_metrics(),
                    "routing_called_tools": "url_extract, web",
                    "routing_successful_tools": "web",
                    "routing_grounding_quality_score": 100,
                    "agent_tool_call_count": 2,
                    "tool_timeout_count": 1,
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)

    def test_extract_called_tools_falls_back_to_sanitized_agent_messages(self) -> None:
        serialized = json.dumps(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "one",
                            "type": "function",
                            "function": {"name": "web", "arguments": "{}"},
                        },
                        {
                            "id": "two",
                            "type": "function",
                            "function": {"name": "url_extract", "arguments": "{}"},
                        },
                    ],
                }
            ]
        )

        self.assertEqual(
            ("web", "url_extract"),
            extract_called_tools({"_diagnostic_agent_messages_json": serialized}),
        )


class LiveBenchmarkFixtureExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_executor_integrates_with_tool_runner(self) -> None:
        runner = build_live_benchmark_fixture_tool_runner()
        calls = [
            _call(
                "web-1",
                "web",
                '{"queries":["latest LumenOS"],"topic":null,"time_range":null}',
            ),
            _call(
                "url-1",
                "url_extract",
                '{"url":"https://bench.nycti.invalid/policy","query":null}',
            ),
            _call("python-1", "calc", '{"code":"result = 9173 * 62011"}'),
            _call("quote-1", "quote", '{"symbols":["ACME"]}'),
            _call(
                "deep-1",
                "deep_research",
                (
                    '{"question":"AtlasDB vs NovaDB","focus":null,"urls":null,'
                    '"symbols":null,"youtube_urls":null,"calculations":null}'
                ),
            ),
        ]

        outcomes = await runner.run(
            calls,
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="benchmark-run",
            step_index=1,
        )

        self.assertEqual([ToolStatus.OK] * 5, [outcome.status for outcome in outcomes])
        self.assertIn("7.4", outcomes[0].content)
        self.assertIn("37", outcomes[1].content)
        self.assertIn("568826903", outcomes[2].content)
        self.assertIn("137.25", outcomes[3].content)
        self.assertIn("AtlasDB", outcomes[4].content)
        self.assertEqual(2, outcomes[4].metrics["deep_research_source_count"])
        self.assertTrue(all(outcome.metrics["live_benchmark_fixture_tool_count"] == 1 for outcome in outcomes))
        self.assertTrue(outcomes[0].provenance)
        self.assertTrue(outcomes[4].provenance)

    async def test_extended_fixture_tools_return_bounded_stable_evidence(self) -> None:
        runner = build_live_benchmark_fixture_tool_runner()
        calls = [
            _call(
                "browser-1",
                "browser_extract",
                (
                    '{"url":"https://bench.nycti.invalid/dashboard",'
                    '"query":null,"headed":false}'
                ),
            ),
            _call(
                "history-1",
                "price_hist",
                (
                    '{"symbol":"ACME","interval":"1day","outputsize":5,'
                    '"start_date":null,"end_date":null}'
                ),
            ),
            _call(
                "annual-1",
                "annual_perf",
                '{"symbols":["ALFA"],"start_year":2024}',
            ),
            _call(
                "transcript-1",
                "yt_transcript",
                '{"url":"https://youtu.be/benchNycti01","query":null}',
            ),
            _call("image-1", "img_search", '{"query":"snowy owl"}'),
            _call(
                "memory-1",
                "memory_search",
                (
                    '{"query":"editor preference","owner_user_ids":null,'
                    '"visibility_scopes":["private"]}'
                ),
            ),
            _call(
                "context-1",
                "channel_ctx",
                '{"mode":"raw","multiplier":1,"expand":false}',
            ),
        ]

        outcomes = await runner.run(
            calls,
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="extended-benchmark-run",
            step_index=1,
        )

        self.assertEqual([ToolStatus.OK] * 7, [outcome.status for outcome in outcomes])
        self.assertIn("12 services operational", outcomes[0].content)
        self.assertIn("132.10", outcomes[1].content)
        self.assertIn("12.5%", outcomes[2].content)
        self.assertIn("shadow traffic", outcomes[3].content)
        self.assertIn("snowy-owl.jpg", outcomes[4].content)
        self.assertIn("Helix", outcomes[5].content)
        self.assertIn("Thursday, June 18", outcomes[6].content)
        self.assertIn("16:00 UTC", outcomes[6].content)
        self.assertIn("Marcus", outcomes[6].content)
        self.assertIn("forced refresh", outcomes[6].content)
        self.assertIn("go/no-go", outcomes[6].content)
        self.assertTrue(all(outcome.provenance for outcome in outcomes[:5]))

    async def test_deep_fixture_combines_url_quote_and_calculation(self) -> None:
        executor = LiveBenchmarkFixtureExecutor()

        result = await executor.execute(
            tool_name="deep_research",
            arguments=(
                '{"question":"Summarize these inputs.","focus":null,'
                '"urls":["https://bench.nycti.invalid/policy"],'
                '"symbols":["ACME"],'
                '"youtube_urls":["https://youtu.be/benchNycti01"],'
                '"calculations":["result=9173*62011"]}'
            ),
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.OK, result.status)
        self.assertIn("37", result.content)
        self.assertIn("137.25", result.content)
        self.assertIn("568826903", result.content)
        self.assertIn("shadow traffic", result.content)
        self.assertEqual(4, result.metrics["deep_research_specialized_call_count"])

    async def test_deep_fixture_rejects_wrong_specialized_inputs(self) -> None:
        result = await LiveBenchmarkFixtureExecutor().execute(
            tool_name="deep_research",
            arguments=(
                '{"question":"summarize","focus":null,'
                '"urls":["https://wrong.invalid"],"symbols":["WRONG"],'
                '"youtube_urls":["https://youtu.be/wrong"],'
                '"calculations":["result=2+2"]}'
            ),
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.ERROR, result.status)
        self.assertNotIn("568826903", result.content)

    async def test_memory_fixture_enforces_owner_and_visibility_scope(self) -> None:
        executor = LiveBenchmarkFixtureExecutor()
        result = await executor.execute(
            tool_name="memory_search",
            arguments=(
                '{"query":"editor preference","owner_user_ids":[999],'
                '"visibility_scopes":["lore"]}'
            ),
            guild_id=None,
            channel_id=None,
            user_id=123,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.EMPTY, result.status)
        self.assertNotIn("Helix", result.content)

        lore = await executor.execute(
            tool_name="memory_search",
            arguments=(
                '{"query":"mascot lore","owner_user_ids":null,'
                '"visibility_scopes":["lore"]}'
            ),
            guild_id=None,
            channel_id=None,
            user_id=123,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )
        shared = await executor.execute(
            tool_name="memory_search",
            arguments=(
                '{"query":"shared project codename","owner_user_ids":null,'
                '"visibility_scopes":["guild_shared"]}'
            ),
            guild_id=None,
            channel_id=None,
            user_id=123,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.OK, lore.status)
        self.assertIn("Nyx", lore.content)
        self.assertEqual(1, lore.metrics["memory_search_lore_result_count"])
        self.assertEqual(ToolStatus.OK, shared.status)
        self.assertIn("Aster", shared.content)
        self.assertEqual(1, shared.metrics["memory_search_guild_shared_result_count"])

    async def test_fixture_executor_rejects_bad_arguments_and_unknown_tools(self) -> None:
        executor = LiveBenchmarkFixtureExecutor()

        invalid = await executor.execute(
            tool_name="calc",
            arguments='{"code":"result = 2 + 2"}',
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )
        unknown = await executor.execute(
            tool_name="not_a_tool",
            arguments="{}",
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.ERROR, invalid.status)
        self.assertEqual(ToolStatus.ERROR, unknown.status)
        self.assertEqual(1, unknown.metrics["live_benchmark_unexpected_tool_count"])

    def test_fixture_advertises_only_tools_it_can_serve(self) -> None:
        names = LiveBenchmarkFixtureExecutor().available_tool_names(
            guild_id=None,
            channel_id=None,
            source_message_id=None,
        )

        self.assertEqual(LIVE_BENCHMARK_FIXTURE_TOOL_NAMES, names)


class LiveBenchmarkRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_filters_repeats_and_observes_each_attempt(self) -> None:
        manifest = load_live_benchmark_manifest()
        observed = []

        async def execute(_case):  # type: ignore[no-untyped-def]
            return LiveBenchmarkExecution(
                answer="568826903",
                metrics={
                    **_fixture_slo_metrics(),
                    "routing_called_tools": "calc",
                    "routing_successful_tools": "calc",
                    "agent_tool_call_count": 1,
                },
            )

        async def observe(attempt):  # type: ignore[no-untyped-def]
            observed.append(attempt.attempt_id)

        result = await run_live_benchmark_suite(
            execute_case=execute,
            manifest=manifest,
            mode="fixtures",
            case_id="fixture-calculation",
            repeats=2,
            on_attempt=observe,
            available_tools=LIVE_BENCHMARK_FIXTURE_TOOL_NAMES,
            batch_id="batch",
        )

        self.assertEqual(2, len(result.attempts))
        self.assertEqual([1, 2], [attempt.attempt_index for attempt in result.attempts])
        self.assertEqual(2, result.count(LiveBenchmarkStatus.PASS))
        self.assertEqual(2, len(observed))
        self.assertTrue(result.passed)

    async def test_unavailable_required_tool_skips_without_calling_model(self) -> None:
        called = False

        async def execute(_case):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            raise AssertionError("must not run")

        result = await run_live_benchmark_suite(
            execute_case=execute,
            manifest=load_live_benchmark_manifest(),
            mode="fixtures",
            case_id="fixture-calculation",
            available_tools=frozenset({"web"}),
        )

        self.assertFalse(called)
        self.assertEqual(LiveBenchmarkStatus.SKIP, result.attempts[0].status)
        self.assertIn("calc", result.attempts[0].evaluation.reason)

    async def test_callback_exception_becomes_error_and_next_repeat_runs(self) -> None:
        calls = 0

        async def execute(_case):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("provider offline")
            return LiveBenchmarkExecution(
                answer="568826903",
                called_tools=("calc",),
                successful_tools=("calc",),
                metrics={**_fixture_slo_metrics(), "agent_tool_call_count": 1},
            )

        result = await run_live_benchmark_suite(
            execute_case=execute,
            manifest=load_live_benchmark_manifest(),
            mode="fixtures",
            case_id="fixture-calculation",
            repeats=2,
        )

        self.assertEqual(
            [LiveBenchmarkStatus.ERROR, LiveBenchmarkStatus.PASS],
            [attempt.status for attempt in result.attempts],
        )
        self.assertIn("RuntimeError", result.attempts[0].evaluation.reason)

    async def test_observer_error_is_reported_without_changing_quality_result(self) -> None:
        async def execute(_case):  # type: ignore[no-untyped-def]
            return LiveBenchmarkExecution(
                answer="568826903",
                called_tools=("calc",),
                successful_tools=("calc",),
                metrics={**_fixture_slo_metrics(), "agent_tool_call_count": 1},
            )

        async def broken_observer(_attempt):  # type: ignore[no-untyped-def]
            raise OSError("database offline")

        result = await run_live_benchmark_suite(
            execute_case=execute,
            manifest=load_live_benchmark_manifest(),
            mode="fixtures",
            case_id="fixture-calculation",
            on_attempt=broken_observer,
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, result.attempts[0].status)
        self.assertEqual(1, len(result.observer_errors))
        self.assertIn("database offline", result.observer_errors[0])

    async def test_cancellation_waits_for_completed_attempt_persistence(self) -> None:
        persistence_started = asyncio.Event()
        persisted: list[str] = []

        async def execute(_case):  # type: ignore[no-untyped-def]
            return LiveBenchmarkExecution(
                answer="568826903",
                called_tools=("calc",),
                successful_tools=("calc",),
                metrics={"agent_tool_call_count": 1},
            )

        async def persist(attempt):  # type: ignore[no-untyped-def]
            persistence_started.set()
            await asyncio.sleep(0.02)
            persisted.append(attempt.attempt_id)

        task = asyncio.create_task(
            run_live_benchmark_suite(
                execute_case=execute,
                manifest=load_live_benchmark_manifest(),
                mode="fixtures",
                case_id="fixture-calculation",
                on_attempt=persist,
            )
        )
        await persistence_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(1, len(persisted))

    async def test_invalid_repeat_and_case_filters_fail_before_model_call(self) -> None:
        async def execute(_case):  # type: ignore[no-untyped-def]
            raise AssertionError("must not run")

        with self.assertRaisesRegex(ValueError, "repeats"):
            await run_live_benchmark_suite(
                execute_case=execute,
                manifest=load_live_benchmark_manifest(),
                repeats=4,
            )
        with self.assertRaisesRegex(ValueError, "No fixtures"):
            await run_live_benchmark_suite(
                execute_case=execute,
                manifest=load_live_benchmark_manifest(),
                case_id="missing",
            )


def _manifest_raw(*, prompt: str = "Short prompt") -> dict[str, object]:
    return {
        "version": 1,
        "description": "test",
        "cases": [
            {
                "id": "case-one",
                "mode": "fixtures",
                "prompt": prompt,
                "checks": {},
            }
        ],
    }


def _call(call_id: str, name: str, arguments: str) -> object:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def _fixture_slo_metrics() -> dict[str, int | str]:
    return {
        "reply_generation_ms": 1_000,
        "agent_model_turn_count": 2,
        "agent_total_tokens": 1_000,
        "agent_stop_reason": "final_text",
    }


if __name__ == "__main__":
    unittest.main()
