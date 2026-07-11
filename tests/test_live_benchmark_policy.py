from __future__ import annotations

import json
import unittest

from nycti.live_benchmarks import (
    LiveBenchmarkCase,
    LiveBenchmarkChecks,
    LiveBenchmarkExecution,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    evaluate_live_benchmark,
    load_live_benchmark_manifest,
    parse_live_benchmark_manifest,
)


class LiveBenchmarkModeDefaultTests(unittest.TestCase):
    def test_mode_defaults_merge_keywise_with_case_overrides(self) -> None:
        manifest = parse_live_benchmark_manifest(
            {
                "version": 1,
                "mode_defaults": {
                    "fixtures": {
                        "metric_max": {
                            "reply_generation_ms": 30_000,
                            "agent_model_turn_count": 3,
                            "agent_total_tokens": 20_000,
                        },
                        "metric_equals": {"agent_stop_reason": "final_text"},
                    },
                    "canaries": {
                        "metric_max": {
                            "reply_generation_ms": 30_000,
                            "agent_model_turn_count": 4,
                            "agent_total_tokens": 25_000,
                        },
                        "metric_equals": {"agent_stop_reason": "final_text"},
                    },
                },
                "cases": [
                    {
                        "id": "fixture-case",
                        "mode": "fixtures",
                        "prompt": "Short?",
                        "checks": {
                            "metric_max": {"agent_model_turn_count": 1},
                        },
                    },
                    {
                        "id": "canary-case",
                        "mode": "canaries",
                        "prompt": "Current?",
                        "checks": {},
                    },
                ],
            }
        )

        fixture = manifest.get_case("fixture-case")
        self.assertEqual(30_000, fixture.checks.metric_max["reply_generation_ms"])
        self.assertEqual(1, fixture.checks.metric_max["agent_model_turn_count"])
        self.assertEqual(20_000, fixture.checks.metric_max["agent_total_tokens"])
        self.assertEqual("final_text", fixture.checks.metric_equals["agent_stop_reason"])
        canary = manifest.get_case("canary-case")
        self.assertEqual(30_000, canary.checks.metric_max["reply_generation_ms"])
        self.assertEqual(4, canary.checks.metric_max["agent_model_turn_count"])
        self.assertEqual(25_000, canary.checks.metric_max["agent_total_tokens"])

    def test_mode_defaults_and_image_policy_keep_strict_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_live_benchmark_manifest(
                _manifest(mode_defaults={"all": {"metric_max": {}}})
            )
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_live_benchmark_manifest(
                _manifest(
                    mode_defaults={
                        "fixtures": {"answer_regex": ["anything"]},
                    }
                )
            )
        invalid_image_policy = _manifest()
        invalid_image_policy["cases"][0]["checks"] = {
            "image_delivery_required": "yes"
        }
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            parse_live_benchmark_manifest(invalid_image_policy)

    def test_checked_in_manifest_applies_slos_to_every_case(self) -> None:
        manifest = load_live_benchmark_manifest()

        for case in manifest.cases:
            with self.subTest(case=case.case_id):
                self.assertIn("reply_generation_ms", case.checks.metric_max)
                self.assertIn("agent_model_turn_count", case.checks.metric_max)
                self.assertIn("agent_total_tokens", case.checks.metric_max)
                self.assertEqual(
                    "final_text",
                    case.checks.metric_equals.get("agent_stop_reason"),
                )

        self.assertEqual(
            10_000,
            manifest.get_case("fixture-quick-recursion").checks.metric_max[
                "reply_generation_ms"
            ],
        )
        self.assertEqual(
            60_000,
            manifest.get_case("canary-deep-openai").checks.metric_max[
                "reply_generation_ms"
            ],
        )

    def test_inherited_slo_is_a_hard_scoring_check(self) -> None:
        case = load_live_benchmark_manifest().get_case("fixture-calculation")
        baseline = {
            "routing_called_tools": "python",
            "routing_successful_tools": "python",
            "agent_tool_call_count": 1,
            "agent_model_turn_count": 2,
            "agent_total_tokens": 1_000,
            "agent_stop_reason": "final_text",
        }

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="568826903",
                metrics={**baseline, "reply_generation_ms": 2_000},
            ),
        )
        too_slow = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="568826903",
                metrics={**baseline, "reply_generation_ms": 30_001},
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, too_slow.status)
        self.assertIn("metric:max:reply_generation_ms", "\n".join(too_slow.failed_checks))


class LiveBenchmarkCanaryPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_live_benchmark_manifest()

    def test_latest_model_canary_rejects_grounded_non_answer(self) -> None:
        case = self.manifest.get_case("canary-openai-latest")
        non_answer = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="I couldn't determine whether GPT-5 is OpenAI's newest model.",
                metrics=_canary_metrics(tool="web"),
            ),
        )
        answered = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI identifies GPT-5.6 as its newest model in the cited release.",
                metrics=_canary_metrics(tool="web"),
            ),
        )
        vague = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI identifies a GPT model as its newest release.",
                metrics=_canary_metrics(tool="web"),
            ),
        )
        stale = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI's newest model is GPT-3.",
                metrics=_canary_metrics(tool="web"),
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, non_answer.status)
        self.assertIn("answer:forbidden", "\n".join(non_answer.failed_checks))
        self.assertEqual(LiveBenchmarkStatus.FAIL, vague.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, stale.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, answered.status)

    def test_news_canary_rejects_old_dated_result(self) -> None:
        case = self.manifest.get_case("canary-openai-news")

        stale = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI announced GPT-3 on March 14, 2020.",
                metrics=_canary_metrics(tool="web"),
            ),
        )
        current = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Today, OpenAI announced a new API feature.",
                metrics=_canary_metrics(tool="web"),
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, stale.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, current.status)

    def test_deep_canary_rejects_unqualified_old_model_list(self) -> None:
        case = self.manifest.get_case("canary-deep-openai")
        metrics = {
            **_canary_metrics(tool="deep_research"),
            "deep_research_query_count": 2,
            "deep_research_source_count": 2,
        }

        stale = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI models include GPT-3.",
                metrics=metrics,
            ),
        )
        current = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="OpenAI's newest release is GPT-5.6, according to the cited sources.",
                metrics=metrics,
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, stale.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, current.status)

    def test_spy_canary_requires_a_labeled_currency_price(self) -> None:
        case = self.manifest.get_case("canary-spy-quote")
        timestamp_only = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="SPY quote checked at 14.30 UTC.",
                metrics=_canary_metrics(tool="quote"),
            ),
        )
        priced = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="SPY last traded at $623.45.",
                metrics=_canary_metrics(tool="quote"),
            ),
        )
        wrong_symbol = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="SPY last traded at $623.45.",
                metrics={
                    **_canary_metrics(tool="quote"),
                    "stock_quote_symbols": "QQQ",
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, timestamp_only.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, priced.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, wrong_symbol.status)


class MigratedBenchmarkCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_live_benchmark_manifest()

    def test_earnings_case_requires_exact_values_and_official_sources(self) -> None:
        case = self.manifest.get_case("fixture-earnings-comparison")
        complete = (
            "NVIDIA (NVDA) — Q1 fiscal 2027, reported May 20, 2026: revenue "
            "$81.615 billion; adjusted EPS $1.87; Q2 guidance $91.0 billion, plus or "
            "minus 2%. https://investor.nvidia.com/news/press-release-details/2026/"
            "NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx\n"
            "AMD — Q1 2026, reported May 5, 2026: revenue $10.253 billion; adjusted "
            "EPS $1.37; Q2 guidance $11.2 billion, plus or minus $300 million. "
            "https://ir.amd.com/news-events/press-releases/detail/1254/"
            "amd-reports-first-quarter-2026-financial-results"
        )
        metrics = _grounded_metrics(tool="web")

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer=complete, metrics=metrics),
        )
        compact = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=(
                    "NVIDIA Q1 fiscal 2027, May 20, 2026: revenue $81.615B, adjusted EPS "
                    "$1.87, guidance $91.0B +/-2%. investor.nvidia.com. "
                    "AMD Q1 2026, May 5, 2026: revenue $10.253B, adjusted EPS $1.37, "
                    "guidance $11.2B +/-$300M. ir.amd.com."
                ),
                metrics=metrics,
            ),
        )
        wrong = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=(
                    "NVIDIA Q1 fiscal 2027 on May 20, 2026: revenue $70 billion, "
                    "adjusted EPS $1.00, guidance $80 billion plus or minus 2%. "
                    "AMD Q1 2026 on May 5, 2026: revenue $9 billion, adjusted EPS "
                    "$1.00, guidance $10 billion plus or minus $300 million. "
                    "https://news.example/nvidia https://news.example/amd"
                ),
                metrics=metrics,
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, compact.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, wrong.status)
        self.assertIn("answer:matches", "\n".join(wrong.failed_checks))

    def test_opaque_version_case_accepts_based_morphology(self) -> None:
        case = self.manifest.get_case("fixture-opaque-version")
        evaluation = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Pyra 3.0 replaces 2.9's mutex-based sessions with lease-based sessions.",
                metrics=_grounded_metrics(tool="web"),
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluation.status)

    def test_context_case_preserves_owners_open_question_and_tool_policy(self) -> None:
        case = self.manifest.get_case("fixture-channel-decision")
        answer = (
            "Final plan: Thursday, June 18 at 16:00 UTC with a 10% canary. "
            "Marcus owns the rollback runbook and drill, due June 16 at 18:00 UTC. "
            "Elena owns the alert dashboard and paging checks, due June 17 at 12:00 UTC. "
            "The unresolved open question is whether mobile clients need a forced refresh. "
            "The go/no-go decision is due June 17 at 15:00 UTC."
        )
        metrics = {
            **_grounded_metrics(tool="channel_ctx"),
            "channel_context_fetch_count": 1,
        }

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer=answer, metrics=metrics),
        )
        used_web = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=answer,
                metrics={
                    **metrics,
                    "routing_called_tools": "channel_ctx, web",
                    "routing_successful_tools": "channel_ctx, web",
                    "agent_tool_call_count": 2,
                },
            ),
        )
        superseded = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=answer + " The earlier Friday blue-green proposal was replaced.",
                metrics=metrics,
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, used_web.status)
        self.assertIn("tool:not_called:web", "\n".join(used_web.failed_checks))
        self.assertEqual(LiveBenchmarkStatus.FAIL, superseded.status)

    def test_spacex_case_rejects_market_cap_stale_listing_and_token_confusion(self) -> None:
        case = self.manifest.get_case("canary-spacex-price")
        metrics = {
            **_grounded_metrics(tool="quote"),
            "stock_quote_count": 1,
            "stock_quote_symbols": "SPCX",
        }

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=(
                    "Ignore the unofficial crypto token: SpaceX (SPCX) stock last traded "
                    "at $42.15 per share."
                ),
                metrics=metrics,
            ),
        )
        market_cap_only = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="SpaceX (SPCX) has a market capitalization of $900 billion.",
                metrics=metrics,
            ),
        )
        stale = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="SpaceX is still private and has no public ticker or stock price.",
                metrics=metrics,
            ),
        )
        token = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="The SPCX crypto token price is $42.15.",
                metrics=metrics,
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, market_cap_only.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, stale.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, token.status)

    def test_semiconductor_case_requires_eight_distinct_tickers_and_valid_quotes(self) -> None:
        case = self.manifest.get_case("canary-semis-sector")
        metrics = {
            **_grounded_metrics(tool="quote"),
            "stock_quote_count": 1,
            "stock_quote_symbol_count": 9,
            "stock_quote_success_symbol_count": 9,
        }
        complete = (
            "Semiconductor stocks quoted today: NVDA $175, TSM $205, AVGO $350, "
            "ASML $390, AMD $185, QCOM $180, TXN $195, AMAT $220, and MU $190."
        )

        passed = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer=complete, metrics=metrics),
        )
        only_seven = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=(
                    "Semiconductor stocks: NVDA NVDA NVDA, TSM, AVGO, ASML, AMD, "
                    "QCOM, and MU; quotes were checked."
                ),
                metrics=metrics,
            ),
        )
        high_market_cap = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=complete + " AMD's market cap is above $585B.",
                metrics=metrics,
            ),
        )
        partial_quotes = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=complete,
                metrics={**metrics, "stock_quote_success_symbol_count": 7},
            ),
        )
        unsynthesized = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=complete + " I found web sources with unsynthesized snippets.",
                metrics=metrics,
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, passed.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, only_seven.status)
        self.assertIn("answer:matches_group:1", "\n".join(only_seven.failed_checks))
        self.assertEqual(LiveBenchmarkStatus.PASS, high_market_cap.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, partial_quotes.status)
        self.assertIn(
            "metric:min:stock_quote_success_symbol_count",
            "\n".join(partial_quotes.failed_checks),
        )
        self.assertEqual(LiveBenchmarkStatus.FAIL, unsynthesized.status)


class LiveBenchmarkImageDeliveryTests(unittest.TestCase):
    def test_image_delivery_accepts_markdown_image_and_bare_image_url(self) -> None:
        case = _image_case()

        markdown_image = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="![Snowy owl](https://images.example/owl.jpg)",
            ),
        )
        bare_url = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="https://images.example/owl.webp?size=large",
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, markdown_image.status)
        self.assertEqual(LiveBenchmarkStatus.PASS, bare_url.status)

    def test_image_delivery_accepts_extensionless_url_from_successful_image_tool(self) -> None:
        case = _image_case()
        signed_url = "https://cdn.example/media?id=owl&signature=abc"

        evaluated = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer=signed_url,
                metrics={
                    "_diagnostic_agent_steps_json": json.dumps(
                        [
                            {
                                "tool_name": "img_search",
                                "status": "ok",
                                "details": {"provenance": [signed_url]},
                            }
                        ]
                    )
                },
            ),
        )

        self.assertEqual(LiveBenchmarkStatus.PASS, evaluated.status)

    def test_image_delivery_rejects_ordinary_source_links_and_pages(self) -> None:
        case = _image_case()

        source_link = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(
                answer="Source: [owl photo](https://images.example/owl.jpg)",
            ),
        )
        article_url = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="https://example.com/snowy-owl-article"),
        )
        disguised_page = evaluate_live_benchmark(
            case,
            LiveBenchmarkExecution(answer="![owl](https://example.com/article)"),
        )

        self.assertEqual(LiveBenchmarkStatus.FAIL, source_link.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, article_url.status)
        self.assertEqual(LiveBenchmarkStatus.FAIL, disguised_page.status)
        self.assertIn("answer:image_delivery", "\n".join(source_link.failed_checks))

    def test_checked_in_image_cases_require_delivery(self) -> None:
        manifest = load_live_benchmark_manifest()

        self.assertTrue(
            manifest.get_case("fixture-image-search").checks.image_delivery_required
        )
        self.assertTrue(
            manifest.get_case("canary-image-search").checks.image_delivery_required
        )


def _manifest(*, mode_defaults: object | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "cases": [
            {
                "id": "case-one",
                "mode": "fixtures",
                "prompt": "Short?",
                "checks": {},
            }
        ],
    }
    if mode_defaults is not None:
        payload["mode_defaults"] = mode_defaults
    return payload


def _canary_metrics(*, tool: str) -> dict[str, int | str]:
    metrics: dict[str, int | str] = {
        "routing_called_tools": tool,
        "routing_successful_tools": tool,
        "routing_grounding_quality_score": 100,
        "agent_tool_call_count": 1,
        "agent_model_turn_count": 2,
        "reply_generation_ms": 2_000,
        "agent_total_tokens": 2_000,
        "agent_stop_reason": "final_text",
    }
    if tool == "quote":
        metrics["stock_quote_symbols"] = "SPY"
    return metrics


def _grounded_metrics(*, tool: str) -> dict[str, int | str]:
    return {
        "routing_called_tools": tool,
        "routing_successful_tools": tool,
        "routing_grounding_quality_score": 100,
        "agent_tool_call_count": 1,
        "agent_model_turn_count": 2,
        "reply_generation_ms": 2_000,
        "agent_total_tokens": 2_000,
        "agent_stop_reason": "final_text",
    }


def _image_case() -> LiveBenchmarkCase:
    return LiveBenchmarkCase(
        case_id="image-delivery",
        mode=LiveBenchmarkMode.FIXTURES,
        prompt="Image?",
        checks=LiveBenchmarkChecks(image_delivery_required=True),
    )


if __name__ == "__main__":
    unittest.main()
