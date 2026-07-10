import unittest
from types import SimpleNamespace

from nycti.llm.client import (
    DEFAULT_PRICING,
    OpenAIClient,
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

    def test_non_reasoning_model_does_not_receive_reasoning_effort(self) -> None:
        request = _build_chat_completion_request_variants(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=700,
            temperature=0.7,
            capabilities=capabilities_for_base_url(None),
            reasoning_effort="high",
        )[0]

        self.assertNotIn("reasoning_effort", request)
        self.assertEqual(0.7, request["temperature"])

    def test_efficiency_feature_uses_separate_reasoning_effort(self) -> None:
        effort = _reasoning_effort_for_feature(
            feature="memory_extract",
            foreground_effort="high",
            efficiency_effort="minimal",
        )

        self.assertEqual(effort, "minimal")

    def test_deep_research_uses_efficiency_reasoning_without_strong_fallback(self) -> None:
        effort = _reasoning_effort_for_feature(
            feature="deep_research_plan",
            foreground_effort="high",
            efficiency_effort="minimal",
        )
        client = object.__new__(OpenAIClient)
        client.settings = SimpleNamespace(
            openai_chat_model="economy-model",
            openai_quick_model=None,
            openai_deep_model=None,
            openai_chat_model_fallbacks=("strong-fallback",),
            openai_memory_model="economy-model",
            openai_vision_model=None,
        )
        client.fallback_client = object()  # type: ignore[assignment]
        client._unhealthy_chat_models_until = {}

        self.assertEqual("minimal", effort)
        self.assertEqual(
            ["economy-model"],
            client._chat_model_candidates(
                "economy-model",
                feature="deep_research_plan",
            ),
        )
        self.assertFalse(
            client._can_use_cross_provider_fallback(
                model="economy-model",
                feature="deep_research_plan",
            )
        )

    def test_answer_plan_reasoning_override_takes_precedence(self) -> None:
        effort = _reasoning_effort_for_feature(
            feature="chat_reply",
            foreground_effort="high",
            efficiency_effort="minimal",
            override="low",
        )

        self.assertEqual(effort, "low")


if __name__ == "__main__":
    unittest.main()
