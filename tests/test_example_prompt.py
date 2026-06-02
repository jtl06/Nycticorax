from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


class ExamplePromptTests(unittest.TestCase):
    def test_example_prompt_matches_generator_output(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "scripts" / "generate_example_prompt.py"
        spec = importlib.util.spec_from_file_location("generate_example_prompt", script_path)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        expected = module.generate_example_prompt()
        actual = (repo_root / "example_prompt.md").read_text(encoding="utf-8")

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
