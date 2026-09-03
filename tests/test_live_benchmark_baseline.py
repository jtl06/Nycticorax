from __future__ import annotations

from datetime import UTC, datetime
import unittest

from nycti.live_benchmark_baseline import (
    build_live_benchmark_baseline,
    compare_live_benchmark_baseline,
)
from nycti.live_benchmarks import (
    LiveBenchmarkAttempt,
    LiveBenchmarkCheckResult,
    LiveBenchmarkEvaluation,
    LiveBenchmarkExecution,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    LiveBenchmarkSuiteResult,
    aggregate_live_benchmark_suite,
    load_live_benchmark_manifest,
)


class LiveBenchmarkAggregateTests(unittest.TestCase):
    def test_aggregate_reports_quality_and_end_to_end_percentiles(self) -> None:
        result = _suite(
            _attempt(latency_ms=100, status=LiveBenchmarkStatus.PASS),
            _attempt(latency_ms=300, status=LiveBenchmarkStatus.FAIL, index=2),
        )

        aggregate = aggregate_live_benchmark_suite(result)

        self.assertEqual(2, aggregate.attempt_count)
        self.assertEqual(0.5, aggregate.pass_rate)
        self.assertEqual(3, aggregate.check_score)
        self.assertEqual(4, aggregate.check_max_score)
        self.assertEqual(0.75, aggregate.check_rate)
        self.assertEqual(200, aggregate.latency_avg_ms)
        self.assertEqual(100, aggregate.latency_p50_ms)
        self.assertEqual(300, aggregate.latency_p90_ms)
        self.assertEqual(900, aggregate.reply_generation_avg_ms)
        self.assertEqual(1.5, aggregate.model_turns_avg)
        self.assertEqual(0.5, aggregate.tool_calls_avg)
        self.assertEqual(4500, aggregate.tokens_avg)


class LiveBenchmarkBaselineTests(unittest.TestCase):
    def test_same_result_matches_its_baseline(self) -> None:
        result = _suite(_attempt(latency_ms=1_000, status=LiveBenchmarkStatus.PASS))
        baseline = build_live_benchmark_baseline(result)

        comparison = compare_live_benchmark_baseline(result, baseline)

        self.assertTrue(comparison.passed)
        self.assertEqual((), comparison.failures)
        self.assertIn("fewer than 3 attempts", comparison.notices[0])

    def test_comparison_reports_quality_and_latency_regressions(self) -> None:
        baseline_result = _suite(
            *(
                _attempt(latency_ms=1_000, status=LiveBenchmarkStatus.PASS, index=index)
                for index in range(1, 4)
            )
        )
        baseline = build_live_benchmark_baseline(baseline_result)
        current = _suite(
            *(
                _attempt(latency_ms=1_600, status=LiveBenchmarkStatus.FAIL, index=index)
                for index in range(1, 4)
            )
        )

        comparison = compare_live_benchmark_baseline(
            current,
            baseline,
            latency_tolerance=0.10,
        )

        self.assertFalse(comparison.passed)
        rendered = "\n".join(comparison.failures)
        self.assertIn("fail_count regressed", rendered)
        self.assertIn("pass_rate regressed", rendered)
        self.assertIn("latency_p50_ms regressed", rendered)
        self.assertIn("previously passing case regressed", rendered)


def _attempt(
    *,
    latency_ms: int,
    status: LiveBenchmarkStatus,
    index: int = 1,
) -> LiveBenchmarkAttempt:
    case = load_live_benchmark_manifest().get_case("fixture-quick-recursion")
    checks = (
        LiveBenchmarkCheckResult("one", True, "ok"),
        LiveBenchmarkCheckResult(
            "two",
            status == LiveBenchmarkStatus.PASS,
            "ok" if status == LiveBenchmarkStatus.PASS else "failed",
        ),
    )
    return LiveBenchmarkAttempt(
        batch_id="batch",
        case=case,
        attempt_index=index,
        evaluation=LiveBenchmarkEvaluation(status=status, checks=checks),
        started_at=datetime.now(UTC),
        latency_ms=latency_ms,
        execution=LiveBenchmarkExecution(
            answer="Recursion calls itself.",
            metrics={
                "active_chat_model": "gpt-5.6-terra",
                "reply_generation_ms": 900,
                "agent_model_turn_count": index,
                "agent_tool_call_count": index - 1,
                "agent_total_tokens": 3_000 * index,
            },
        ),
    )


def _suite(*attempts: LiveBenchmarkAttempt) -> LiveBenchmarkSuiteResult:
    return LiveBenchmarkSuiteResult(
        batch_id="batch",
        manifest_version=19,
        mode=LiveBenchmarkMode.FIXTURES,
        attempts=tuple(attempts),
        started_at=datetime.now(UTC),
        latency_ms=sum(attempt.latency_ms for attempt in attempts),
    )


if __name__ == "__main__":
    unittest.main()
