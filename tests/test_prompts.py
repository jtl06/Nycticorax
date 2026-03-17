import unittest
from importlib.resources import files

from nycti.prompts import get_system_prompt


class PromptLoadingTests(unittest.TestCase):
    def test_system_prompt_loaded_from_prompt_markdown(self) -> None:
        get_system_prompt.cache_clear()
        expected = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8").strip()
        self.assertEqual(get_system_prompt(), expected)
        self.assertTrue(expected)


if __name__ == "__main__":
    unittest.main()
