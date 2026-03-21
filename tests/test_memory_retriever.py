import unittest

from nycti.memory.scoring import cosine_similarity


class MemoryRetrieverTests(unittest.TestCase):
    def test_cosine_similarity_prefers_aligned_vectors(self) -> None:
        high = cosine_similarity([1.0, 0.0], [1.0, 0.0])
        low = cosine_similarity([1.0, 0.0], [0.0, 1.0])
        self.assertGreater(high, low)

    def test_cosine_similarity_handles_missing_vectors(self) -> None:
        self.assertEqual(cosine_similarity(None, [1.0, 0.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], None), 0.0)
        self.assertEqual(cosine_similarity([1.0], [1.0, 2.0]), 0.0)


if __name__ == "__main__":
    unittest.main()
