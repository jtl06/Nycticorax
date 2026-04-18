import unittest

from nycti.memory.filtering import (
    contains_sensitive_pattern,
    lexical_similarity,
    should_skip_memory_extraction,
)
from nycti.memory.profile import (
    clean_profile_markdown,
    should_attempt_profile_update,
    strip_noncaller_profile_lines,
)


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
        self.assertLessEqual(len(clean_profile_markdown("x" * 1000)), 600)

    def test_should_attempt_profile_update_skips_non_self_mention_prompt(self) -> None:
        self.assertFalse(
            should_attempt_profile_update("@gts81 (user_id=456) what plan does he want")
        )

    def test_should_attempt_profile_update_allows_self_signal_with_mention(self) -> None:
        self.assertTrue(
            should_attempt_profile_update("I am coordinating with @gts81 (user_id=456) on this")
        )

    def test_strip_noncaller_profile_lines_removes_mention_markers(self) -> None:
        cleaned = strip_noncaller_profile_lines(
            "- Likes concise replies\n- GTS is @gts81 (user_id=456)\n- Works on Nycti"
        )
        self.assertEqual(cleaned, "- Likes concise replies\n- Works on Nycti")


if __name__ == "__main__":
    unittest.main()
