import unittest

from nycti.llm.client import (
    DEFAULT_PRICING,
    _build_chat_completion_request_variants,
    _reasoning_effort_for_feature,
)
from nycti.llm.provider_policy import capabilities_for_base_url


class ReasoningRequestTests(unittest.TestCase):
    def test_provider_switch_models_have_explicit_pricing(self) -> None:
        self.assertIn("gpt-5.6-luna", DEFAULT_PRICING)
        self.assertIn("deepseek-ai/DeepSeek-V4-Pro", DEFAULT_PRICING)

    def test_openai_reasoning_request_sets_effort_and_omits_temperature(self) -> None:
        request = _build_chat_completion_request_variants(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=700,
            temperature=0.7,
            capabilities=capabilities_for_base_url(None),
            reasoning_effort="high",
        )[0]

        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(request["max_completion_tokens"], 700)
        self.assertNotIn("temperature", request)

    def test_efficiency_feature_uses_separate_reasoning_effort(self) -> None:
        effort = _reasoning_effort_for_feature(
            feature="memory_extract",
            foreground_effort="high",
            efficiency_effort="minimal",
        )

        self.assertEqual(effort, "minimal")


if __name__ == "__main__":
    unittest.main()
