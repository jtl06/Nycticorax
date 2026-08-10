import unittest
from unittest.mock import patch

from nycti.python_sandbox import PythonSandboxError, run_python_sandbox


class PythonSandboxTests(unittest.TestCase):
    def test_run_python_sandbox_returns_print_and_result(self) -> None:
        result = run_python_sandbox(
            "print('hello')\nresult = round(math.sqrt(81), 2)",
            timeout_seconds=1,
            max_output_chars=1000,
        )

        self.assertIn("hello", result.output)
        self.assertIn("result = 9.0", result.output)

    def test_run_python_sandbox_allows_safe_math_imports(self) -> None:
        imported = run_python_sandbox(
            "import math as m\nfrom statistics import mean\nresult = (m.sqrt(81), mean([2, 4, 6]))",
            timeout_seconds=2,
            max_output_chars=1000,
        )

        self.assertIn("result = (9.0, 4)", imported.output)

    def test_run_python_sandbox_allows_numpy_and_networkx(self) -> None:
        numeric = run_python_sandbox(
            "import numpy as np\na = np.array([1, 2, 3, 4])\nresult = (float(np.mean(a)), np.cumsum(a).tolist())",
            timeout_seconds=3,
            max_output_chars=1000,
        )
        graph = run_python_sandbox(
            "import networkx as nx\ng = nx.Graph()\ng.add_edges_from([('a', 'b'), ('b', 'c')])\n"
            "result = nx.shortest_path(g, 'a', 'c')",
            timeout_seconds=3,
            max_output_chars=1000,
        )

        self.assertIn("result = (2.5, [1, 3, 6, 10])", numeric.output)
        self.assertIn("result = ['a', 'b', 'c']", graph.output)

    def test_run_python_sandbox_blocks_unsafe_imports_and_io(self) -> None:
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox("import os", timeout_seconds=1, max_output_chars=1000)
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox("open('/etc/passwd').read()", timeout_seconds=1, max_output_chars=1000)
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox(
                "import numpy as np\nresult = np.load('/etc/passwd')",
                timeout_seconds=2,
                max_output_chars=1000,
            )

    def test_run_python_sandbox_times_out(self) -> None:
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox("while True:\n    pass", timeout_seconds=0.01, max_output_chars=1000)

    def test_run_python_sandbox_uses_isolated_interpreter(self) -> None:
        with patch("nycti.python_sandbox.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"ok": true, "output": "result = 4", "elapsed_ms": 1}'
            run.return_value.stderr = ""

            run_python_sandbox("result = 4", timeout_seconds=1, max_output_chars=1000)

        command = run.call_args.args[0]
        self.assertIn("-I", command)
        self.assertNotIn("-c", command)
        self.assertEqual("1", run.call_args.kwargs["env"]["OPENBLAS_NUM_THREADS"])


if __name__ == "__main__":
    unittest.main()
