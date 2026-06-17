import ast
from pathlib import Path
import unittest


class DiscordBenchmarkCommandTests(unittest.TestCase):
    def test_benchmarks_do_not_force_search_requested(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "nycti" / "discord" / "core.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        benchmark_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_benchmark"
        ]

        self.assertGreaterEqual(len(benchmark_calls), 3)
        for call in benchmark_calls:
            search_requested = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "search_requested"),
                None,
            )
            self.assertIsInstance(search_requested, ast.Constant)
            self.assertIs(search_requested.value, False)


if __name__ == "__main__":
    unittest.main()
