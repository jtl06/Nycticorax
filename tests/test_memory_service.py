import ast
from pathlib import Path
import unittest


class MemoryServiceSourceTests(unittest.TestCase):
    def test_record_usage_is_imported_when_called(self) -> None:
        source = Path("src/nycti/memory/service.py").read_text()
        tree = ast.parse(source)
        imports_record_usage = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "nycti.usage"
            and any(alias.name == "record_usage" for alias in node.names)
            for node in tree.body
        )
        calls_record_usage = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "record_usage"
            for node in ast.walk(tree)
        )

        self.assertTrue(calls_record_usage)
        self.assertTrue(imports_record_usage)


if __name__ == "__main__":
    unittest.main()
