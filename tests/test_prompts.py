import unittest
from importlib.resources import files

from nycti.prompts import get_system_prompt


class PromptLoadingTests(unittest.TestCase):
    def test_system_prompt_loaded_from_prompt_markdown(self) -> None:
        get_system_prompt.cache_clear()
        expected = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8").strip()
        self.assertEqual(get_system_prompt(), expected)
        self.assertTrue(expected)

    def test_system_prompt_avoids_clarifai_blocked_latex_delimiter_examples(self) -> None:
        prompt = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8")

        self.assertIn("Discord does not render LaTeX", prompt)
        self.assertNotIn("`\\(...\\)`", prompt)
        self.assertNotIn("`\\[...\\]`", prompt)
        self.assertNotIn("`$$...$$`", prompt)

    def test_system_prompt_allows_labeled_speculative_guesses(self) -> None:
        prompt = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8")

        self.assertIn("For speculative asks", prompt)
        self.assertIn("do not hard-refuse", prompt)
        self.assertIn("best-effort guess", prompt)

    def test_system_prompt_covers_short_discord_grounding_cases(self) -> None:
        prompt = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8")
        short_discord_cases = {
            "use search...": "If the user says \"use search\"",
            "nvda ath when": "For live/current asks",
            "how did spacex do today": "how did X do today",
            "did spacex ipo": "For IPO, public/private status, listing status, ticker identity",
            "spacex + tesla valuation": "For combined public/private company valuations",
            "mangos?": "If a needed tool fails or gives weak evidence",
            "stop searching the same thing": "Do not repeat the same or near-identical tool request",
        }

        for _message, expected_rule in short_discord_cases.items():
            with self.subTest(message=_message):
                self.assertIn(expected_rule, prompt)

    def test_system_prompt_has_medium_length_agent_rules_without_tool_catalog(self) -> None:
        prompt = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8")

        self.assertGreaterEqual(len(prompt), 3000)
        self.assertLessEqual(len(prompt), 5000)
        self.assertIn("The current request is the main instruction", prompt)
        self.assertIn("Use tools when freshness, precision, or grounding materially matters", prompt)
        self.assertNotIn("Available tools this turn:", prompt)
        self.assertNotIn("web, quote, channel_ctx", prompt)


if __name__ == "__main__":
    unittest.main()
