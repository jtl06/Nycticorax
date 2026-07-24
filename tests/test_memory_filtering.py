import unittest
from types import SimpleNamespace

from nycti.memory.filtering import (
    contains_sensitive_pattern,
    has_guild_lore_signal,
    lexical_similarity,
    should_skip_memory_extraction,
)
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.profile import (
    clean_profile_markdown,
    strip_noncaller_profile_lines,
)
from nycti.memory.visibility import MemoryVisibility
from nycti.llm.types import LLMResult, LLMUsage


class MemoryFilteringTests(unittest.TestCase):
    def test_sensitive_content_is_rejected(self) -> None:
        self.assertTrue(contains_sensitive_pattern("my password is swordfish123"))
        self.assertEqual(should_skip_memory_extraction("my API key is sk-1234567890abc")[1], "sensitive")

    def test_low_value_chatter_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("lol")
        self.assertTrue(skip)
        self.assertEqual(reason, "low_value")

    def test_preference_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I prefer crunchy tacos over soft tacos.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_project_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I'm working on a retro racing game after work.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_explicit_memory_update_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("Remember that I no longer work at Acme.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_guild_lore_requires_explicit_group_language(self) -> None:
        self.assertTrue(has_guild_lore_signal("We always call broken deploys a moon launch."))
        self.assertTrue(has_guild_lore_signal("That is a running joke in this server."))
        self.assertFalse(has_guild_lore_signal("Mat likes mechanical keyboards."))

    def test_goal_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want to get a job at Optiver.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_transient_phone_plan_shopping_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want a phone plan that gives me a good iPhone deal.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_transient_promo_hunting_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want a free Apple Watch promotion.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_link_request_memory_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("Please give me official Cartier product page links for the Tank and Santos.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_lexical_similarity_prefers_overlap(self) -> None:
        high = lexical_similarity(
            "What games do I like to play on Friday nights?",
            "Prefers co-op horror games on Friday nights.",
            ["games", "friday"],
        )
        low = lexical_similarity(
            "What games do I like to play on Friday nights?",
            "Owns a blue road bike for weekend rides.",
            ["bike"],
        )
        self.assertGreater(high, low)

    def test_clean_profile_markdown_normalizes_and_caps(self) -> None:
        cleaned = clean_profile_markdown("```markdown\n-  likes   direct answers\n- works on Nycti\n```")
        self.assertEqual(cleaned, "- likes direct answers\n- works on Nycti")
        self.assertLessEqual(len(clean_profile_markdown("x" * 1000)), 800)

    def test_strip_noncaller_profile_lines_removes_mention_markers(self) -> None:
        cleaned = strip_noncaller_profile_lines(
            "- Likes concise replies\n- GTS is @gts81 (user_id=456)\n- Works on Nycti"
        )
        self.assertEqual(cleaned, "- Likes concise replies\n- Works on Nycti")


class MemoryExtractorScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_group_convention_can_become_lore(self) -> None:
        client = _MemoryLLMClient(
            '{"should_store":true,"confidence":0.95,"category":"lore",'
            '"memory":"Calls broken deploys moon launches","tags":["deploy"],'
            '"visibility":"lore","contains_sensitive":false}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            client,
        )

        candidate, _ = await extractor.extract(
            current_message="We always call broken deploys a moon launch.",
            recent_context="GTS: the deploy failed again",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(MemoryVisibility.LORE, candidate.suggested_visibility)
        self.assertIn("current message is authored by the memory owner", client.system_prompt)

    async def test_personal_fact_cannot_be_auto_promoted_to_lore(self) -> None:
        client = _MemoryLLMClient(
            '{"should_store":true,"confidence":0.95,"category":"preference",'
            '"memory":"Prefers dark mode","tags":["theme"],'
            '"visibility":"lore","contains_sensitive":false}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            client,
        )

        candidate, _ = await extractor.extract(
            current_message="I prefer dark mode.",
            recent_context="Lucis: everyone should use it",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(MemoryVisibility.PRIVATE, candidate.suggested_visibility)

    async def test_string_false_is_not_treated_as_true(self) -> None:
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            _MemoryLLMClient(
                '{"should_store":"false","confidence":0.99,"category":"preference",'
                '"memory":"Should not store","tags":[],"visibility":"private",'
                '"contains_sensitive":"false"}'
            ),
        )

        candidate, _ = await extractor.extract(
            current_message="This is a long enough candidate message.",
            recent_context="",
        )

        self.assertIsNone(candidate)


class _MemoryLLMClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.system_prompt = ""

    def is_model_available(self, _model: str) -> bool:
        return True

    async def complete_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.system_prompt = kwargs["messages"][0]["content"]
        return LLMResult(
            text=self.text,
            usage=LLMUsage(
                feature="memory_extract",
                model="memory-model",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
