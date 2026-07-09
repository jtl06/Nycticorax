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

    def test_run_python_sandbox_blocks_imports_and_open(self) -> None:
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox("import os", timeout_seconds=1, max_output_chars=1000)
        with self.assertRaises(PythonSandboxError):
            run_python_sandbox("open('/etc/passwd').read()", timeout_seconds=1, max_output_chars=1000)

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


if __name__ == "__main__":
    unittest.main()
