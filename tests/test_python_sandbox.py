import unittest

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


if __name__ == "__main__":
    unittest.main()
