import ast
from pathlib import Path
import unittest


class ProfileUpdateSourceTests(unittest.TestCase):
    def test_profile_update_is_gated_in_background_memory_task(self) -> None:
        source = Path("src/nycti/bot.py").read_text()
        tree = ast.parse(source)

        store_method = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "_store_memory_background"
            ),
            None,
        )
        self.assertIsNotNone(store_method)
        assert store_method is not None

        call_names = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(store_method)
            if isinstance(node, ast.Call)
            and (isinstance(node.func, ast.Attribute) or isinstance(node.func, ast.Name))
        }
        self.assertIn("maybe_update_personal_profile", call_names)
        self.assertIn("has_useful_memory_signal", call_names)
        self.assertIn("select_related_memory_user_ids", call_names)

        has_gate_var = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "should_update_profile" for target in node.targets)
            for node in ast.walk(store_method)
        )
        self.assertTrue(has_gate_var)

        def _if_uses_should_update_profile(test: ast.AST) -> bool:
            if isinstance(test, ast.Name):
                return test.id == "should_update_profile"
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                return _if_uses_should_update_profile(test.operand)
            if isinstance(test, ast.BoolOp):
                return any(_if_uses_should_update_profile(value) for value in test.values)
            return False

        gated_if = any(
            isinstance(node, ast.If) and _if_uses_should_update_profile(node.test)
            for node in ast.walk(store_method)
        )
        self.assertTrue(gated_if)


if __name__ == "__main__":
    unittest.main()
