import unittest

from nycti.benchmarks import (
    EARNINGS_BENCHMARK_PROMPT,
    format_earnings_benchmark_score,
    score_earnings_benchmark,
)


COMPLETE_ANSWER = """
NVIDIA reported Q1 FY2027 results on May 20, 2026. Actual revenue was $81.6 billion and
adjusted diluted EPS was $1.87. Next-quarter revenue guidance is $91 billion, plus or minus 2%.
Source: https://investor.nvidia.com/news/results-q1

AMD reported Q1 2026 results on May 5, 2026. Actual revenue was $10.3 billion and
non-GAAP diluted EPS was $1.37. Next-quarter guidance: $11.2 billion, plus or minus $300 million.
Source: https://ir.amd.com/news-events/results-q2
"""


class EarningsBenchmarkTests(unittest.TestCase):
    def test_prompt_requires_all_scored_fields_and_actuals(self) -> None:
        self.assertIn("report date", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("actual revenue", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("adjusted/non-GAAP diluted EPS", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("revenue guidance", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("official investor-relations or SEC source URL", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("Do not substitute analyst estimates", EARNINGS_BENCHMARK_PROMPT)
        self.assertIn("Never construct or guess an investor-relations URL", EARNINGS_BENCHMARK_PROMPT)

    def test_complete_grounded_answer_gets_full_score(self) -> None:
        score = score_earnings_benchmark(COMPLETE_ANSWER)

        self.assertEqual(10, score.completeness_points)
        self.assertEqual(10, score.correctness_checks)
        self.assertEqual((), score.missing)
        self.assertEqual((), score.incorrect)

    def test_score_reports_missing_company_fields(self) -> None:
        score = score_earnings_benchmark(
            "NVIDIA reported Q1 fiscal results on May 20, 2026 with revenue of $44 billion."
        )

        self.assertLess(score.completeness_points, 10)
        self.assertIn("NVIDIA adjusted EPS", score.missing)
        self.assertIn("AMD actual revenue", score.missing)

    def test_unavailable_guidance_does_not_get_credit_from_nearby_actual_revenue(self) -> None:
        answer = COMPLETE_ANSWER.replace(
            "Next-quarter revenue guidance is $91 billion, plus or minus 2%.",
            "Next-quarter guidance: Specific revenue outlook was not extracted.",
        ).replace(
            "Next-quarter guidance: $11.2 billion, plus or minus $300 million.",
            "Next-quarter guidance: Specific revenue outlook was unavailable.",
        )

        score = score_earnings_benchmark(answer)

        self.assertIn("NVIDIA revenue guidance", score.missing)
        self.assertIn("AMD revenue guidance", score.missing)
        self.assertEqual(8, score.completeness_points)

    def test_wrong_values_are_present_but_fail_correctness(self) -> None:
        answer = COMPLETE_ANSWER.replace("$81.6 billion", "$71.6 billion").replace(
            "$1.37",
            "$1.17",
        )

        score = score_earnings_benchmark(answer)

        self.assertEqual(10, score.completeness_points)
        self.assertEqual(8, score.correctness_checks)
        self.assertIn("NVIDIA actual revenue", score.incorrect)
        self.assertIn("AMD adjusted EPS", score.incorrect)

    def test_formatter_includes_quality_and_runtime_metrics(self) -> None:
        rendered = format_earnings_benchmark_score(
            score_earnings_benchmark(COMPLETE_ANSWER),
            {
                "agent_model_turn_count": 2,
                "agent_tool_call_count": 1,
                "chat_total_tokens": 1234,
                "end_to_end_ms": 4567,
            },
        )

        self.assertIn("completeness=10/10", rendered)
        self.assertIn("correctness_checks=10/10", rendered)
        self.assertIn("incorrect=none", rendered)
        self.assertIn("turns=2 tools=1 retries=0 tokens=1234 latency_ms=4567", rendered)


if __name__ == "__main__":
    unittest.main()
