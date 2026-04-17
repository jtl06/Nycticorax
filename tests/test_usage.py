import ast
from pathlib import Path
import unittest


class UsageSourceTests(unittest.TestCase):
    def test_usage_prune_function_exists_and_deletes_old_rows(self) -> None:
        source = Path("src/nycti/usage.py").read_text()
        tree = ast.parse(source)

        function_nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
        prune_fn = next((node for node in function_nodes if node.name == "prune_usage_events_before"), None)
        self.assertIsNotNone(prune_fn)
        assert prune_fn is not None

        uses_delete = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "delete"
            for node in ast.walk(prune_fn)
        )
        self.assertTrue(uses_delete)

        compares_created_at = any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "created_at"
            and any(isinstance(op, ast.Lt) for op in node.ops)
            for node in ast.walk(prune_fn)
        )
        self.assertTrue(compares_created_at)


if __name__ == "__main__":
    unittest.main()
