import unittest

from cinclus.memory.filtering import (
    contains_sensitive_pattern,
    lexical_similarity,
    should_skip_memory_extraction,
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


if __name__ == "__main__":
    unittest.main()
