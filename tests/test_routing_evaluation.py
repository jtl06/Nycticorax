from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from nycti.chat.routing_evaluation import (
    ROUTING_TELEMETRY_SCHEMA,
    RoutingCase,
    RoutingObservation,
    evaluate_routing,
    load_routing_corpus,
    observe_static_routes,
    record_runtime_routing_metrics,
)
from nycti.chat.run_state import AgentRun, EvidenceMode, ToolOutcome, ToolStatus
from nycti.chat.orchestrator_support import constrain_answer_plan_to_runtime
from nycti.chat.tool_eligibility import select_answer_plan


class RoutingCorpusTests(unittest.TestCase):
    def test_labeled_corpus_has_no_static_exposure_or_promotion_regressions(self) -> None:
        cases = load_routing_corpus()

        result = evaluate_routing(cases, observe_static_routes(cases))

        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(0, result.exposure_miss_count)
        self.assertEqual(0, result.promotion_miss_count)
        self.assertEqual(0, result.unexpected_promotion_count)
        self.assertEqual(0, result.observed_call_count)
        self.assertIsNone(result.mean_grounding_quality)

    def test_corpus_covers_prior_false_positive_multilingual_and_novel_cases(self) -> None:
        categories = {case.category for case in load_routing_corpus()}

        self.assertIn("prior_freshness_miss", categories)
        self.assertIn("stable_false_positive", categories)
        self.assertIn("multilingual_freshness", categories)
        self.assertIn("novel_named_version", categories)
        self.assertIn("observed_benchmark_miss", categories)

    def test_observed_calls_latency_and_grounding_quality_are_scored(self) -> None:
        cases = (
            RoutingCase(
                case_id="grounded",
                prompt="latest fact",
                category="test",
                grounding_required=True,
                required_exposed_tools=frozenset({"web"}),
                acceptable_called_tools=frozenset({"web"}),
                expected_promoted_tools=frozenset({"web"}),
            ),
            RoutingCase(
                case_id="stable",
                prompt="explain recursion",
                category="test",
                grounding_required=False,
                required_exposed_tools=frozenset(),
                acceptable_called_tools=frozenset(),
                expected_promoted_tools=frozenset(),
            ),
        )
        observations = (
            RoutingObservation(
                case_id="grounded",
                exposed_tool_names=frozenset({"web"}),
                promoted_tool_names=frozenset({"web"}),
                called_tool_names=frozenset(),
                latency_ms=120,
                grounding_quality_score=0.5,
            ),
            RoutingObservation(
                case_id="stable",
                exposed_tool_names=frozenset({"web"}),
                promoted_tool_names=frozenset(),
                called_tool_names=frozenset(),
                latency_ms=80,
                grounding_quality_score=1.0,
            ),
        )

        result = evaluate_routing(cases, observations)

        self.assertEqual(1, result.call_miss_count)
        self.assertEqual(100.0, result.mean_latency_ms)
        self.assertEqual(0.75, result.mean_grounding_quality)
        self.assertEqual(0.5, result.grounding_quality_pass_rate)

    def test_invalid_corpus_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"cases": [{"id": "missing fields"}]}', encoding="utf-8")

            with self.assertRaises(ValueError):
                load_routing_corpus(path)


class RuntimeRoutingTelemetryTests(unittest.TestCase):
    def test_runtime_metrics_cover_exposure_calls_misses_latency_and_quality(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Find the latest official result",
            guild_id=1,
        )
        outcome = ToolOutcome(
            call_id="call-1",
            tool_name="web",
            arguments='{"queries":["official result"]}',
            status=ToolStatus.OK,
            content="Official result: https://example.com/result",
            provenance=("https://example.com/result",),
        )
        run = AgentRun(messages=[], answer_plan=plan, evidence_mode=EvidenceMode.CITED)
        run.attempted_tools.add("web")
        run.successful_tools.add("web")
        run.outcomes.append(outcome)
        metrics: dict[str, int | str] = {}

        record_runtime_routing_metrics(
            metrics,
            run=run,
            answer_text="Supported by https://example.com/result",
        )

        self.assertEqual(len(plan.direct_tool_names), metrics["routing_exposed_tool_count"])
        self.assertEqual("web", metrics["routing_called_tools"])
        self.assertEqual(0, metrics["routing_exposure_miss_count"])
        self.assertEqual(0, metrics["routing_tool_call_miss_count"])
        self.assertEqual(1, metrics["routing_grounding_expected"])
        self.assertEqual(0, metrics["routing_grounding_miss_count"])
        self.assertEqual(100, metrics["routing_grounding_quality_score"])
        self.assertEqual(
            {
                "routing_exposed_tools",
                "routing_exposed_tool_count",
                "routing_deferred_tools",
                "routing_promoted_tools",
                "routing_unavailable_promoted_tools",
                "routing_called_tools",
                "routing_called_tool_count",
                "routing_exposure_miss_count",
                "routing_tool_call_miss_count",
                "routing_grounding_expected",
                "routing_grounding_miss_count",
                "routing_latency_ms",
                "routing_grounding_quality_score",
            },
            set(ROUTING_TELEMETRY_SCHEMA),
        )

    def test_unavailable_promotion_and_unrelated_call_remain_misses(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Find the latest official result",
            guild_id=1,
        )
        runtime_names = plan.direct_tool_names - {"web"}
        runner = SimpleNamespace(
            executor=SimpleNamespace(
                available_tool_names=lambda **_kwargs: runtime_names,
            )
        )
        plan = constrain_answer_plan_to_runtime(
            plan,
            runner,
            guild_id=1,
            channel_id=2,
            source_message_id=3,
        )
        run = AgentRun(messages=[], answer_plan=plan)
        run.attempted_tools.add("memory_search")
        metrics: dict[str, int | str] = {}

        record_runtime_routing_metrics(metrics, run=run, answer_text="A stale answer.")

        self.assertEqual(("web",), plan.unavailable_promoted_tool_names)
        self.assertEqual("web", metrics["routing_unavailable_promoted_tools"])
        self.assertEqual(1, metrics["routing_exposure_miss_count"])
        self.assertEqual(1, metrics["routing_tool_call_miss_count"])
        self.assertEqual(1, metrics["routing_grounding_expected"])
        self.assertEqual(1, metrics["routing_grounding_miss_count"])
        self.assertEqual(0, metrics["routing_grounding_quality_score"])

    def test_grounding_quality_requires_visible_citations_only_in_cited_mode(self) -> None:
        plan, _ = select_answer_plan(request_text="Find the latest official result", guild_id=1)
        outcome = ToolOutcome(
            call_id="call-1",
            tool_name="web",
            arguments='{"queries":["official result"]}',
            status=ToolStatus.OK,
            content="Official result.",
            provenance=("https://example.com/result",),
        )
        scores: dict[EvidenceMode, int | str] = {}
        for mode in (EvidenceMode.INTERNAL, EvidenceMode.CITED):
            run = AgentRun(messages=[], answer_plan=plan, evidence_mode=mode)
            run.attempted_tools.add("web")
            run.successful_tools.add("web")
            run.outcomes.append(outcome)
            metrics: dict[str, int | str] = {}
            record_runtime_routing_metrics(metrics, run=run, answer_text="Supported claim.")
            scores[mode] = metrics["routing_grounding_quality_score"]

        self.assertEqual("unscored", scores[EvidenceMode.INTERNAL])
        self.assertEqual(0, scores[EvidenceMode.CITED])

    def test_quick_non_grounded_answer_without_evidence_remains_unscored(self) -> None:
        plan, _ = select_answer_plan(request_text="Tell me a joke", guild_id=1)
        run = AgentRun(messages=[], answer_plan=plan)
        metrics: dict[str, int | str] = {}

        record_runtime_routing_metrics(metrics, run=run, answer_text="A joke.")

        self.assertEqual(0, metrics["routing_grounding_expected"])
        self.assertEqual(0, metrics["routing_grounding_miss_count"])
        self.assertEqual("unscored", metrics["routing_grounding_quality_score"])

    def test_ambiguous_default_without_promotion_remains_unscored(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Help me brainstorm names for this project",
            guild_id=1,
        )
        run = AgentRun(messages=[], answer_plan=plan)
        metrics: dict[str, int | str] = {}

        record_runtime_routing_metrics(metrics, run=run, answer_text="Some names.")

        self.assertEqual("grounded", str(plan.profile))
        self.assertFalse(plan.promoted_tool_names)
        self.assertEqual(0, metrics["routing_grounding_expected"])
        self.assertEqual("unscored", metrics["routing_grounding_quality_score"])

    def test_labeled_grounding_expectation_scores_missing_evidence(self) -> None:
        plan, _ = select_answer_plan(request_text="What is QuokkaDB 7.3?", guild_id=1)
        run = AgentRun(messages=[], answer_plan=plan)
        metrics: dict[str, int | str] = {"routing_grounding_expected": 1}

        record_runtime_routing_metrics(metrics, run=run, answer_text="A stale answer.")

        self.assertEqual(1, metrics["routing_grounding_expected"])
        self.assertEqual(1, metrics["routing_grounding_miss_count"])
        self.assertEqual(0, metrics["routing_grounding_quality_score"])


if __name__ == "__main__":
    unittest.main()
