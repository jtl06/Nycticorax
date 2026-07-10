from pathlib import Path
import unittest


class DiscordBenchmarkCommandTests(unittest.TestCase):
    def test_benchmarks_do_not_use_hidden_search_controls(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "nycti" / "discord" / "core.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("search_requested", source)
        self.assertNotIn("fast_search_requested", source)
        self.assertIn("register_live_benchmark_commands", source)
        self.assertIn("isolated_benchmark=True", source)
        self.assertIn("persist_memory=False", source)


if __name__ == "__main__":
    unittest.main()
